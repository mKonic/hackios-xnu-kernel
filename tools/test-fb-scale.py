#!/usr/bin/env python3
"""Check MiniFramebufferScale.h against the scalar loops it replaced, pixel for pixel, on the host."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "xnu/files/iokit/Kernel/MiniFramebufferScale.h"

source = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "MiniFramebufferScale.h"

/* MiniIOMobileFramebuffer.cpp's sample_at() */
static int64_t sample_at(int64_t i, int64_t in, int64_t out)
{
    int64_t at = ((2 * i + 1) * in - out) * 128 / out;
    return at < 0 ? 0 : at > (in - 1) * 256 ? (in - 1) * 256 : at;
}

static uint32_t seed = 1;
static uint32_t next(void) { seed = seed * 1103515245u + 12345u; return seed >> 8; }

/* A picture with flat areas (most of a screen) and noise, so both paths of every loop run. */
static void picture(uint32_t *p, uint32_t w, uint32_t h, uint32_t stride)
{
    for (uint32_t y = 0; y < h; y++)
        for (uint32_t x = 0; x < w; x++)
            p[y * stride + x] = (x / 37 + y / 23) % 3 == 0 ? 0xff123456u : (x / 50) % 2 ? next() | 0xff000000u : next();
}

/* The loops as they were in drawRows(), scaled rows from y0 to y1 */
static void reference(const uint32_t *pixels, uint32_t stride, int32_t sx, int32_t sy, int32_t sw, int32_t sh,
                      int32_t px0, int32_t py0, int32_t px1, int32_t py1, int32_t cx0, int32_t cx1,
                      const uint32_t *column, const uint16_t *weight_of, uint32_t *panel, uint32_t row_words, uint32_t *mixed)
{
    for (int32_t y = py0; y < py1; y++) {
        int64_t at = sample_at(y - py0, sh, py1 - py0);
        uint32_t row = (uint32_t)(at >> 8), weight = (uint32_t)(at & 255);
        const uint32_t *upper = pixels + (size_t)(sy + row) * stride + sx, *from;
        if (weight == 0 || row + 1 >= (uint32_t)sh) {
            from = upper;
        } else {
            const uint32_t *lower = pixels + (size_t)(sy + row + 1) * stride + sx;
            uint32_t first = column[cx0], last = column[cx1 - 1] + 1 < (uint32_t)sw ? column[cx1 - 1] + 1 : column[cx1 - 1];
            for (uint32_t c = first; c <= last; c++) {
                uint32_t a = upper[c], b = lower[c];
                mixed[c] = a == b ? a : blend(a, b, weight);
            }
            from = mixed;
        }
        uint32_t *out = panel + (size_t)y * row_words + cx0;
        for (uint32_t i = 0, n = (uint32_t)(cx1 - cx0); i < n; i++) {
            uint32_t c = column[cx0 + i], w = weight_of[cx0 + i], a = from[c];
            out[i] = w == 0 || a == from[c + 1] ? a : blend(a, from[c + 1], w);
        }
    }
}

/* The same through the header, as drawRows() now calls it */
static void vector(const uint32_t *pixels, uint32_t stride, int32_t sx, int32_t sy, int32_t sw, int32_t sh,
                   int32_t px0, int32_t py0, int32_t px1, int32_t py1, int32_t cx0, int32_t cx1,
                   const uint32_t *column, const uint16_t *weight_of, uint32_t *panel, uint32_t row_words, uint32_t *mixed)
{
    for (int32_t y = py0; y < py1; y++) {
        int64_t at = sample_at(y - py0, sh, py1 - py0);
        uint32_t row = (uint32_t)(at >> 8), weight = (uint32_t)(at & 255);
        const uint32_t *upper = pixels + (size_t)(sy + row) * stride + sx, *from;
        if (weight == 0 || row + 1 >= (uint32_t)sh) {
            from = upper;
        } else {
            const uint32_t *lower = pixels + (size_t)(sy + row + 1) * stride + sx;
            uint32_t first = column[cx0], last = column[cx1 - 1] + 1 < (uint32_t)sw ? column[cx1 - 1] + 1 : column[cx1 - 1];
            mixRows(upper, lower, mixed, first, last, weight);
            from = mixed;
        }
        scaleRow(from, panel + (size_t)y * row_words + cx0, column + cx0, weight_of + cx0, (uint32_t)(cx1 - cx0), (uint32_t)sw);
    }
}

typedef void (*scaler)(const uint32_t *, uint32_t, int32_t, int32_t, int32_t, int32_t, int32_t, int32_t, int32_t, int32_t,
                       int32_t, int32_t, const uint32_t *, const uint16_t *, uint32_t *, uint32_t, uint32_t *);

static double one(scaler f, uint32_t sw, uint32_t sh, uint32_t pw, uint32_t ph, int32_t px0, int32_t px1, int32_t py0,
                  int32_t py1, uint32_t sx, uint32_t sy, uint32_t *panel, int rounds)
{
    uint32_t stride = sw + sx, *pixels = malloc((size_t)stride * (sh + sy) * 4), *column = calloc(pw, 4);
    uint16_t *weights = calloc(pw, 2);
    uint32_t *mixed = malloc((size_t)sw * 4);       /* (as blendRow: exactly a row) */
    int32_t cx0 = px0 < 0 ? 0 : px0, cx1 = (uint32_t)px1 > pw ? (int32_t)pw : px1;
    struct timespec a, b;

    picture(pixels, sw + sx, sh + sy, stride);
    for (int32_t x = cx0; x < cx1; x++) {
        int64_t at = sample_at(x - px0, sw, px1 - px0);
        column[x] = (uint32_t)(at >> 8);
        weights[x] = (uint16_t)(column[x] + 1 < sw ? at & 255 : 0);
    }
    clock_gettime(CLOCK_MONOTONIC, &a);
    for (int r = 0; r < rounds; r++)
        f(pixels, stride, sx, sy, sw, sh, px0, py0, px1, py1, cx0, cx1, column, weights, panel, pw, mixed);
    clock_gettime(CLOCK_MONOTONIC, &b);
    free(pixels); free(column); free(weights); free(mixed);
    return ((b.tv_sec - a.tv_sec) * 1e3 + (b.tv_nsec - a.tv_nsec) / 1e6) / rounds;
}

static void compare(uint32_t sw, uint32_t sh, uint32_t pw, uint32_t ph, int32_t px0, int32_t px1, int32_t py0, int32_t py1,
                    uint32_t sx, uint32_t sy)
{
    uint32_t *x = calloc((size_t)pw * ph, 4), *y = calloc((size_t)pw * ph, 4);
    uint32_t keep = seed;

    one(reference, sw, sh, pw, ph, px0, px1, py0, py1, sx, sy, x, 1);
    seed = keep;
    one(vector, sw, sh, pw, ph, px0, px1, py0, py1, sx, sy, y, 1);
    if (memcmp(x, y, (size_t)pw * ph * 4)) {
        for (size_t i = 0; i < (size_t)pw * ph; i++)
            if (x[i] != y[i]) { printf("%ux%u -> %ux%u: first difference at panel %zu,%zu: %08x vs %08x\n", sw, sh, pw, ph, i % pw, i / pw, x[i], y[i]); break; }
        exit(1);
    }
    free(x); free(y);
}

int main(void)
{
    /* the iPhone picture on the K90 panel; with fbscale=2; on redwood's panel (smaller); odd layers */
    compare(1125, 2436, 1156, 2510, 0, 1156, 0, 2510, 0, 0);
    compare(750, 1624, 1156, 2510, 0, 1156, 0, 2510, 0, 0);
    compare(1125, 2436, 1080, 2400, 0, 1080, 0, 2400, 0, 0);
    compare(301, 97, 1156, 400, 13, 330, 5, 104, 3, 2);
    compare(5, 3, 64, 16, -7, 40, 0, 9, 1, 0);
    compare(1125, 300, 1156, 320, -20, 1150, 0, 310, 0, 0);
    compare(1123, 50, 1156, 60, 0, 1156, 0, 60, 0, 0);      /* (a row whose last four are cut short) */
    double old = 0, now = 0;
    uint32_t *panel = calloc((size_t)1156 * 2510, 4);
    old = one(reference, 1125, 2436, 1156, 2510, 0, 1156, 0, 2510, 0, 0, panel, 10);
    now = one(vector, 1125, 2436, 1156, 2510, 0, 1156, 0, 2510, 0, 0, panel, 10);
    printf("fb scale: ok; a frame on this host %.2f ms before, %.2f ms now\n", old, now);
    return 0;
}
'''

with tempfile.TemporaryDirectory() as tmp:
    c = Path(tmp) / "t.c"
    exe = Path(tmp) / "t"
    c.write_text(source)
    for flags in (["-fsanitize=address,undefined", "-fno-sanitize-recover=all"], []):
        subprocess.run(["cc", "-std=gnu11", "-O2", "-Wall", *flags, "-I", str(HEADER.parent), "-o", str(exe), str(c)], check=True)
        subprocess.run([str(exe)], check=True)
