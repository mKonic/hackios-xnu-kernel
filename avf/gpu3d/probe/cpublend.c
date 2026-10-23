/*
 * The software side of the compositing comparison (bench.c): what a CPU compositor does for
 * one full-screen layer, a scaled texture blended source-over onto the frame. Built with NEON
 * (the rest of the probe is -mgeneral-regs-only), so clang vectorizes the blend the way a
 * tuned software rasterizer would.
 */
#include <stdint.h>

void cpu_blend_layer(uint8_t *dst, uint32_t w, uint32_t h, const uint8_t *tex, uint32_t tw, uint32_t th,
		     uint8_t *row, const uint16_t *xmap)
{
	for (uint32_t y = 0; y < h; y++) {
		const uint32_t *src = (const uint32_t *)(tex + (uint64_t)(y * th / h) * tw * 4);
		uint32_t *r = (uint32_t *)row;
		for (uint32_t x = 0; x < w; x++)
			r[x] = src[xmap[x]];
		uint8_t *d = dst + (uint64_t)y * w * 4;
		for (uint32_t i = 0; i < w * 4; i += 4) {
			uint32_t a = row[i + 3], ia = 255 - a;
			d[i + 0] = (uint8_t)((row[i + 0] * a + d[i + 0] * ia + 128) * 257 >> 16);
			d[i + 1] = (uint8_t)((row[i + 1] * a + d[i + 1] * ia + 128) * 257 >> 16);
			d[i + 2] = (uint8_t)((row[i + 2] * a + d[i + 2] * ia + 128) * 257 >> 16);
			d[i + 3] = 255;
		}
	}
}
