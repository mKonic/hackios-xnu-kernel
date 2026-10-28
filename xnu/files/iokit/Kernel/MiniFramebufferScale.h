/*
 * The bilinear scaling of MiniIOMobileFramebuffer.cpp's drawRows(), a row at a time: mixRows()
 * makes a row of the picture from the two around where a panel row falls, scaleRow() a panel row
 * from that. Four pixels at a time where it can, with the processor's vector unit (the compiler's
 * own vector types: -mkernel builds see no arm_neon.h); the result is exactly blend()'s for every
 * pixel either way (tools/test-fb-scale.py).
 *
 * Kept apart from the driver so that the host can build it as it is.
 */
#ifndef _MINI_FRAMEBUFFER_SCALE_H
#define _MINI_FRAMEBUFFER_SCALE_H

#include <stdint.h>

/*
 * Two pixels mixed: `w` of 256 of the second. Two of the four channels at a time, in one word.
 */
static inline uint32_t
blend(uint32_t a, uint32_t b, uint32_t w)
{
	uint32_t rb = ((a & 0xff00ff) * (256 - w) + (b & 0xff00ff) * w) >> 8 & 0xff00ff;
	uint32_t ga = ((a >> 8 & 0xff00ff) * (256 - w) + (b >> 8 & 0xff00ff) * w) & 0xff00ff00;

	return rb | ga;
}

typedef uint8_t  fb_u8x16 __attribute__((vector_size(16)));
typedef uint8_t  fb_u8x8 __attribute__((vector_size(8)));
typedef uint16_t fb_u16x8 __attribute__((vector_size(16)));
typedef uint16_t fb_u16x4 __attribute__((vector_size(8)));

/* blend() of four pixels: `wl` the weights of the channels of the first two, `wh` of the last two. */
static inline fb_u8x16
blend4(fb_u8x16 a, fb_u8x16 b, fb_u16x8 wl, fb_u16x8 wh)
{
	fb_u16x8 al = __builtin_convertvector(__builtin_shufflevector(a, a, 0, 1, 2, 3, 4, 5, 6, 7), fb_u16x8);
	fb_u16x8 ah = __builtin_convertvector(__builtin_shufflevector(a, a, 8, 9, 10, 11, 12, 13, 14, 15), fb_u16x8);
	fb_u16x8 bl = __builtin_convertvector(__builtin_shufflevector(b, b, 0, 1, 2, 3, 4, 5, 6, 7), fb_u16x8);
	fb_u16x8 bh = __builtin_convertvector(__builtin_shufflevector(b, b, 8, 9, 10, 11, 12, 13, 14, 15), fb_u16x8);
	/* (at most 255 * 256: in 16 bits) */
	fb_u8x8 lo = __builtin_convertvector((al * (256 - wl) + bl * wl) >> 8, fb_u8x8);
	fb_u8x8 hi = __builtin_convertvector((ah * (256 - wh) + bh * wh) >> 8, fb_u8x8);

	return __builtin_shufflevector(lo, hi, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15);
}

static inline fb_u8x16
load4(const uint32_t * p)
{
	fb_u8x16 v;

	__builtin_memcpy(&v, p, sizeof(v));
	return v;
}

static inline void
store4(uint32_t * p, fb_u8x16 v)
{
	__builtin_memcpy(p, &v, sizeof(v));
}

/* Columns `first` to `last` of a row `w` of 256 of the way from `upper` to `lower`. */
static inline void
mixRows(const uint32_t * upper, const uint32_t * lower, uint32_t * __restrict mixed, uint32_t first, uint32_t last,
    uint32_t w)
{
	const fb_u16x8 none = { 0, 0, 0, 0, 0, 0, 0, 0 };
	fb_u16x8 weight = none + (uint16_t) w;
	uint32_t c = first;

	for (; c + 3 <= last; c += 4) {
		store4(mixed + c, blend4(load4(upper + c), load4(lower + c), weight, weight));
	}
	for (; c <= last; c++) {
		uint32_t a = upper[c], b = lower[c];

		mixed[c] = a == b ? a : blend(a, b, w);         /* (most of a screen is the same as next to it) */
	}
}

/*
 * `n` panel pixels from a row of `width`: the ith is pixel column[i] mixed with the next,
 * weights[i] of 256 of the next (0 where there is none). Four at a time where they take four
 * pixels side by side, as all do but those where the panel repeats a pixel of the picture.
 */
static inline void
scaleRow(const uint32_t * from, uint32_t * __restrict out, const uint32_t * __restrict column,
    const uint16_t * __restrict weights, uint32_t n, uint32_t width)
{
	uint32_t i = 0;

	for (; i + 4 <= n; i += 4) {
		uint32_t c = column[i];

		/* (column[] goes up by the same one or two steps all along: three in three is one each) */
		if (column[i + 3] == c + 3 && c + 4 < width) {
			fb_u16x4 w;

			__builtin_memcpy(&w, weights + i, sizeof(w));
			store4(out + i, blend4(load4(from + c), load4(from + c + 1), __builtin_shufflevector(w, w, 0, 0, 0, 0, 1, 1, 1, 1),
			    __builtin_shufflevector(w, w, 2, 2, 2, 2, 3, 3, 3, 3)));
			continue;
		}
		for (uint32_t k = i; k < i + 4; k++) {
			uint32_t a = from[column[k]], wk = weights[k];

			out[k] = wk == 0 || a == from[column[k] + 1] ? a : blend(a, from[column[k] + 1], wk);
		}
	}
	for (; i < n; i++) {
		uint32_t a = from[column[i]], w = weights[i];

		out[i] = w == 0 || a == from[column[i] + 1] ? a : blend(a, from[column[i] + 1], w);
	}
}

#endif /* _MINI_FRAMEBUFFER_SCALE_H */
