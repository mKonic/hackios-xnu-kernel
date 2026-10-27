/*
 * MiniIOSurface: a stand-in for Apple's closed IOSurface.kext, enough of it for the iOS
 * window server and its clients to share pixel buffers on a machine without Apple hardware.
 *
 * The protocol (user client selectors, traps and the layout of the structures the kernel
 * hands to IOSurface.framework) follows iOS 16.5 (20F66) and iOS 26.6.2 (23G90).
 */
#ifndef _IOKIT_MINIIOSURFACE_H
#define _IOKIT_MINIIOSURFACE_H

#include <IOKit/IOService.h>
#include <IOKit/IOUserClient.h>
#include <IOKit/IOBufferMemoryDescriptor.h>

#include "MiniDrivers.h"

#include "MiniIOSurfaceABI.h"

/* Common shared fields retain these offsets; ABI helpers access YCbCr and attachments. */
struct minisurf_shared {
	uint64_t registryID;                    /* 0x00 */
	uint16_t accelID;                       /* 0x08 */
	uint16_t accelSubID;                    /* 0x0a */
	uint32_t seed;                          /* 0x0c changes when somebody has written the pixels */
	uint32_t reserved10;
	uint8_t  tiled;                         /* 0x14 */
	uint8_t  reserved15[3];
	uint64_t useCount;                      /* 0x18 two 32-bit counters, one per category */
	uint32_t valuesSeed;                    /* 0x20 */
	uint32_t ycbcrMatrix;                   /* 0x24 */
	uint32_t valuesSize;                    /* 0x28 room needed to fetch the values; 0 = none */
	uint8_t  pixelSizeCasting;              /* 0x2c */
	uint8_t  displayable;                   /* 0x2d */
	uint8_t  sysMemOnly;                    /* 0x2e */
	uint8_t  reserved2f;
	uint8_t  attachments[0x70];             /* 0x30 the "bulk attachments" */
};

struct minisurf_aux {
	uint64_t timestamps[4];
	uint64_t detachModeCode;
};

#define MINISURF_AUX_OFFSET     0x400           /* of the aux structure inside the shared page */

class MiniIOSurface : public OSObject
{
	OSDeclareDefaultStructors(MiniIOSurface);

public:
	uint32_t                        sid;
	minisurf_abi                    sharedABI;      /* fixed by the first successful client mapping */
	uint32_t                        holders;        /* clients, send rights, kernel users */
	IOBufferMemoryDescriptor *      memory;         /* one page of shared state, then the pixels */
	uint8_t *                       kbase;          /* the same, in the kernel */
	/*
	 * ... unless the surface was made around memory its maker has already (IOSurfaceAddress):
	 * then the pixels are that memory, held in place and seen from the kernel as well, and
	 * `memory` is the page of shared state alone.
	 */
	IOMemoryDescriptor *            client;
	IOMemoryMap *                   clientMap;
	task_t                          clientTask;     /* whose memory it is (not held: only compared) */
	mach_vm_address_t               clientAddress;  /* ... and where it is there */
	uint8_t *                       kpixels;        /* where the pixel memory starts in the kernel, either way */
	uint32_t                        drawn;          /* the GPU's last submission that names the pixels (0: none; RedwoodGPU.cpp) */
	struct minisurf_result          info;           /* addresses not filled in */
	struct minisurf_plane           planes[MINISURF_MAX_PLANES];
	OSDictionary *                  values;
	uint32_t                        useCount[2];
	uint32_t                        purgeable;
	bool                            traced;         /* made by a program of boot-arg surftrace_of: its ports are said too */

	virtual bool
	init(void) APPLE_KEXT_OVERRIDE
	{
		sharedABI = MINISURF_UNCLAIMED;
		return OSObject::init();
	}
	virtual void free(void) APPLE_KEXT_OVERRIDE;

	struct minisurf_shared *
	shared(void) const
	{
		return (struct minisurf_shared *) kbase;
	}
	struct minisurf_aux *
	aux(void) const
	{
		return (struct minisurf_aux *) (kbase + MINISURF_AUX_OFFSET);
	}
	/* The first byte of the pixels (the "base address" clients see). */
	uint8_t *
	pixels(void) const
	{
		return kpixels + info.offset;
	}
	void updateValuesState(void);
};

/*
 * For other parts of the kernel (the framebuffer).
 * A surface found or made here is held until MiniIOSurfaceDrop().
 */
MiniIOSurface * MiniIOSurfaceCopy(uint32_t sid);
/*
 * What making and freeing surfaces has cost since this was last asked: how many, microseconds
 * each; how many got memory that was kept from one before.
 */
void            MiniIOSurfaceCost(uint32_t * made, uint64_t * making, uint32_t * freed, uint64_t * freeing, uint32_t * again);
/* Clear the memory that is kept of surfaces that are gone, for the next ones: when the caller has a moment. */
void            MiniIOSurfaceClearKept(void);
/* Wait, for so many milliseconds at most, until the GPU is done with a surface's pixels (RedwoodGPU.cpp). */
bool            MiniIOSurfaceDrawn(MiniIOSurface * surface, uint64_t milliseconds);
MiniIOSurface * MiniIOSurfaceCreateSimple(uint32_t width, uint32_t height, uint32_t pixelFormat);
void            MiniIOSurfaceDrop(MiniIOSurface * surface);
/* A Mach send right for the surface in the task (what IOSurfaceCreateMachPort gives); 0 on failure. */
mach_port_name_t MiniIOSurfaceMakeSendRight(task_t task, MiniIOSurface * surface);


#endif /* _IOKIT_MINIIOSURFACE_H */
