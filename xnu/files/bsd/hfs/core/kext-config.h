/*
 * HFS+ built into the kernel (HackiOS): Apple's hfs-715.100.10 core as a writable filesystem for
 * /private/var on a virtio disk. The smallest configuration: no data protection classes, no
 * compression, no quotas, no named streams -- a volume this kernel makes and mounts for itself.
 */
#ifndef _hfs_config_
#define _hfs_config_

/*
 * HFS+ copies across neighbouring members on purpose (a catalog record's userInfo and finderInfo as one
 * 32 bytes, its two forks as one): written as a kext, it was checked object by object; inside the
 * kernel (XNU_KERNEL_PRIVATE) the checked string functions go member by member and panic on that.
 * Unchecked here, as before it was taken in.
 */
#undef _FORTIFY_SOURCE
#define _FORTIFY_SOURCE 0

#define HFS_COMPRESSION 1
#define FIFO 1
#define QUOTA 0
#define CONFIG_PROTECT 0

/* names this XNU (8796) has for what hfs-715 calls by newer ones */
#define IOMallocDataShareable(size)        IOMallocData(size)
#define IOFreeDataShareable(ptr, size)     IOFreeData(ptr, size)
#define sysctlbyname(n, o, ol, nw, nl)     kernel_sysctlbyname(n, o, ol, nw, nl)
#define ubc_create_upl(vp, off, sz, upl, pl, fl) ubc_create_upl_kernel(vp, off, sz, upl, pl, fl, VM_KERN_MEMORY_FILE)
#include <IOKit/IOLib.h>
#include <sys/sysctl.h>
int kernel_sysctlbyname(const char *name, void *oldp, size_t *oldlenp, void *newp, size_t newlen);

#endif
