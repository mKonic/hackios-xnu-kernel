#ifndef _MINI_IOSURFACE_ABI_H
#define _MINI_IOSURFACE_ABI_H

#include <stddef.h>
#include <stdint.h>

#define MINISURF_MAX_PLANES     4
#define MINISURF_RESULT_MAX     0xa68           /* iOS 16.5 result capacity */

/* Internal plane description and iOS 16.5 wire layout (0x80 bytes). */
struct minisurf_plane {
	uint32_t width;                         /* 0x00 */
	uint32_t height;                        /* 0x04 */
	uint32_t reserved08;
	uint32_t offset;                        /* 0x0c from the base address */
	uint32_t bytesPerRow;                   /* 0x10 */
	uint32_t size;                          /* 0x14 */
	uint16_t bytesPerElement;               /* 0x18 */
	uint8_t  elementWidth;                  /* 0x1a */
	uint8_t  elementHeight;                 /* 0x1b */
	uint8_t  componentCount;                /* 0x1c */
	uint8_t  addressFormat;                 /* 0x1d */
	uint8_t  reserved1e[2];
	uint8_t  componentBitDepth[4];          /* 0x20 */
	uint8_t  componentBitOffset[4];         /* 0x24 */
	uint8_t  componentName[4];              /* 0x28 */
	uint8_t  componentType[4];              /* 0x2c */
	uint8_t  componentRange[4];             /* 0x30 */
	uint8_t  reserved34[0x20];
	uint8_t  compression;                   /* 0x54: 1 = compressed, then there is no base address */
	uint8_t  reserved55[0x1b];
	uint32_t extendedPixels[4];             /* 0x70 */
};

/* Internal description and iOS 16.5 result header, followed by at least one plane. */
struct minisurf_result {
	uint64_t base;                          /* 0x00 where the memory is mapped in the client */
	uint64_t shared;                        /* 0x08 struct minisurf_shared, mapped in the client */
	uint64_t aux;                           /* 0x10 struct minisurf_aux, mapped in the client */
	uint32_t sid;                           /* 0x18 */
	uint32_t allocSize;                     /* 0x1c */
	uint32_t width;                         /* 0x20 */
	uint32_t height;                        /* 0x24 */
	uint32_t bytesPerRow;                   /* 0x28 */
	uint32_t offset;                        /* 0x2c */
	uint32_t pixelFormat;                   /* 0x30 */
	uint32_t reserved34;
	uint32_t planeCount;                    /* 0x38 */
	uint32_t reserved3c;
	uint16_t bytesPerElement;               /* 0x40 */
	uint8_t  elementWidth;                  /* 0x42 */
	uint8_t  elementHeight;                 /* 0x43 */
	uint32_t cacheMode;                     /* 0x44 */
	uint32_t lockSeed;                      /* 0x48 the client keeps the seed of its last lock here */
	uint8_t  subsampling;                   /* 0x4c */
	uint8_t  prohibitUseCount;              /* 0x4d */
	uint16_t reserved4e;
	uint32_t parentID;                      /* 0x50 */
	uint32_t sliceCount;                    /* 0x54 */
	uint64_t traceID;                       /* 0x58 */
	uint64_t protectionOptions;             /* 0x60 */
};


/* The internal description retains 20F66 fields; 23G90 widens its wire sizes. */
enum minisurf_abi { MINISURF_UNCLAIMED, MINISURF_ABI16, MINISURF_ABI26 };

static inline minisurf_abi
minisurfInitABI(uint32_t size)
{
    return size == 0x18 ? MINISURF_ABI16 : size == 0x28 ? MINISURF_ABI26 : MINISURF_UNCLAIMED;
}

static inline void
minisurfPut32(uint8_t * out, unsigned offset, uint32_t value)
{
    __builtin_memcpy(out + offset, &value, sizeof(value));
}

static inline void
minisurfPut64(uint8_t * out, unsigned offset, uint64_t value)
{
    __builtin_memcpy(out + offset, &value, sizeof(value));
}

static inline bool
minisurfInitReply(void * output, uint32_t size, uint32_t allocMask, uint32_t rowMask, uint32_t maxDimension)
{
    minisurf_abi abi = minisurfInitABI(size);
    if (output == NULL || abi == MINISURF_UNCLAIMED) {
        return false;
    }
    uint8_t * out = (uint8_t *) output;
    __builtin_memset(out, 0, size);
    minisurfPut32(out, 0, allocMask);
    minisurfPut32(out, 4, rowMask);
    uint16_t version = 1;
    if (abi == MINISURF_ABI26) {
        minisurfPut64(out, 8, (uint64_t) maxDimension * 16);
        minisurfPut64(out, 0x10, maxDimension);
        minisurfPut64(out, 0x18, maxDimension);
        __builtin_memcpy(out + 0x22, &version, sizeof(version));
    } else {
        minisurfPut32(out, 8, maxDimension * 16);
        minisurfPut32(out, 0xc, maxDimension);
        minisurfPut32(out, 0x10, maxDimension);
        __builtin_memcpy(out + 0x16, &version, sizeof(version));
    }
    return true;
}

static inline uint32_t
minisurfResultSize(minisurf_abi abi, uint32_t planeCount)
{
    if (planeCount > MINISURF_MAX_PLANES) {
        return 0;
    }
    uint32_t planes = planeCount ? planeCount : 1;
    return abi == MINISURF_ABI26 ? 0x88 + planes * 0x98 :
        abi == MINISURF_ABI16 ? sizeof(minisurf_result) + planes * sizeof(minisurf_plane) : 0;
}

/* Returns zero before writing if the output cannot hold the complete description. */
static inline uint32_t
minisurfWriteResult(void * output, uint32_t capacity, minisurf_abi abi,
    const minisurf_result * info, const minisurf_plane * planes)
{
    uint32_t size = minisurfResultSize(abi, info->planeCount);
    if (output == NULL || size == 0 || capacity < size) {
        return 0;
    }
    uint8_t * out = (uint8_t *) output;
    if (abi == MINISURF_ABI16) {
        __builtin_memcpy(out, info, sizeof(*info));
        __builtin_memcpy(out + sizeof(*info), planes, size - sizeof(*info));
        return size;
    }
    __builtin_memset(out, 0, size);
    minisurfPut64(out, 0, info->base);
    minisurfPut64(out, 8, info->shared);
    minisurfPut64(out, 0x10, info->aux);
    minisurfPut32(out, 0x18, info->sid);
    minisurfPut64(out, 0x20, info->allocSize);
    minisurfPut64(out, 0x28, info->width);
    minisurfPut64(out, 0x30, info->height);
    minisurfPut64(out, 0x38, info->bytesPerRow);
    minisurfPut64(out, 0x40, info->offset);
    minisurfPut32(out, 0x48, info->pixelFormat);
    minisurfPut32(out, 0x58, info->planeCount);
    __builtin_memcpy(out + 0x60, &info->bytesPerElement, 4);
    minisurfPut32(out, 0x64, info->cacheMode);
    minisurfPut32(out, 0x68, info->lockSeed);
    out[0x6c] = info->subsampling;
    out[0x6d] = info->prohibitUseCount;
    minisurfPut32(out, 0x70, info->parentID);
    minisurfPut32(out, 0x74, info->sliceCount);
    minisurfPut64(out, 0x78, info->traceID);
    minisurfPut64(out, 0x80, info->protectionOptions);
    uint32_t count = info->planeCount ? info->planeCount : 1;
    for (uint32_t i = 0; i < count; ++i) {
        uint8_t * p = out + 0x88 + i * 0x98;
        minisurfPut64(p, 0, planes[i].width);
        minisurfPut64(p, 8, planes[i].height);
        minisurfPut64(p, 0x18, planes[i].offset);
        minisurfPut64(p, 0x20, planes[i].bytesPerRow);
        minisurfPut64(p, 0x28, planes[i].size);
        __builtin_memcpy(p + 0x30, &planes[i].bytesPerElement, 0x1c);
        p[0x6c] = planes[i].compression;
        __builtin_memcpy(p + 0x88, planes[i].extendedPixels, sizeof(planes[i].extendedPixels));
    }
    return size;
}

static inline bool
minisurfCanMap(minisurf_abi surface, minisurf_abi client)
{
    return (client == MINISURF_ABI16 || client == MINISURF_ABI26) &&
        (surface == MINISURF_UNCLAIMED || surface == client);
}

static inline uint8_t *
minisurfAttachments(void * shared, minisurf_abi abi)
{
    return (uint8_t *) shared + (abi == MINISURF_ABI26 ? 0x34 : 0x30);
}

static inline void
minisurfSetYCbCrMatrix(void * shared, minisurf_abi abi, uint32_t value)
{
    if (abi == MINISURF_ABI26) {
        ((uint8_t *) shared)[0x6e] = (uint8_t) value;
    } else {
        minisurfPut32((uint8_t *) shared, 0x24, value);
    }
}

/* 23G90 selector 27 adds seven attachment fields and moves the request suffix. */
static inline bool
minisurfBulkInput(minisurf_abi abi, const uint8_t * in, size_t inSize,
    uint64_t * size, uint64_t * mask, uint32_t * sid)
{
    unsigned suffix = abi == MINISURF_ABI26 ? 0x88 : 0x70;
    unsigned limit = abi == MINISURF_ABI26 ? 0x84 : 0x70;
    if (in == NULL || inSize < suffix + 0x18) {
        return false;
    }
    __builtin_memcpy(size, in + suffix, sizeof(*size));
    __builtin_memcpy(mask, in + suffix + 8, sizeof(*mask));
    __builtin_memcpy(sid, in + suffix + 16, sizeof(*sid));
    return *size <= limit;
}

static inline void
minisurfApplyBulk(void * shared, minisurf_abi abi, const uint8_t * in, uint64_t size, uint64_t mask)
{
    static const struct { uint8_t from, to; } fields[] = {
        {0x00, 0x20}, {0x20, 0x30}, {0x30, 0x38}, {0x38, 0x39}, {0x39, 0x3a},
        {0x3a, 0x3b}, {0x3b, 0x3c}, {0x3c, 0x3d}, {0x3d, 0x3e}, {0x3e, 0x3f},
        {0x3f, 0x40}, {0x40, 0x58}, {0x58, 0x5c}, {0x5c, 0x64}, {0x64, 0x65},
        {0x68, 0x70}, {0x70, 0x74}, {0x65, 0x66}, {0x66, 0x67}, {0x74, 0x80},
        {0x80, 0x82}, {0x82, 0x84},
    };
    uint8_t * out = minisurfAttachments(shared, abi);
    unsigned count = abi == MINISURF_ABI26 ? 22 : 15;
    for (unsigned i = 0; i < count; ++i) {
        if ((mask & (1ULL << i)) && fields[i].to <= size) {
            __builtin_memcpy(out + fields[i].from, in + fields[i].from, fields[i].to - fields[i].from);
        }
    }
}

#endif /* _MINI_IOSURFACE_ABI_H */
