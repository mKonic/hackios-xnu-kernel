#ifndef _MINI_FRAMEBUFFER_SWAP_H
#define _MINI_FRAMEBUFFER_SWAP_H

#include <stddef.h>
#include <stdint.h>

/* Queue offsets match the fields read from the iOS 16.5 (20F66) request. */
#define SWAP_SIZE               0x46c
#define SWAP_ID                 0x50
#define SWAP_SURFACE(layer)     (0x54 + 4 * (layer))    /* surface number, 0 = none */
#define SWAP_SOURCE(layer)      (0x64 + 16 * (layer))   /* x, y, width, height in the surface */
#define SWAP_ENABLED(layer)     (0xa4 + 4 * (layer))
#define SWAP_DEST(layer)        (0xc4 + 16 * (layer))   /* x, y, width, height on the display */
#define SWAP_LAYERS_SET         0x104                   /* bit per layer given in this swap */
#define SWAP_LAYERS_CLEARED     0x108
#define SWAP_LAYERS             4

/*
 * The 23G90 request is 0x588 bytes; all fields above move by 0x48 bytes.
 * A SWAP_SIZE copy from that offset ends at 0x4b4, within the request.
 * Only these fields share the queue layout; later protocol fields are unused.
 */
static inline const uint8_t *
miniFramebufferSwapPayload(const void * input, uint32_t size)
{
	if (input == NULL) {
		return NULL;
	}
	if (size == SWAP_SIZE) {
		return (const uint8_t *) input;
	}
	if (size == 0x588) {
		return (const uint8_t *) input + 0x48;
	}
	return NULL;
}

#endif /* _MINI_FRAMEBUFFER_SWAP_H */
