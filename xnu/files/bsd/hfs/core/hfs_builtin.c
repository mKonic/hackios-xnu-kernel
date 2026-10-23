/*
 * HFS+ built into the kernel (HackiOS): what hfs_iokit.cpp does for the kext, in C, for a kernel
 * that carries the filesystem itself. hfs_builtin_start() registers it with VFS (bsd_init calls it
 * after VFS is up); the three helpers the core asks the IOKit side for are answered for a virtual
 * machine: no ejectable media, no CoreStorage content to search, no platform serial number.
 */
#include <sys/mount.h>
#include <sys/vnode.h>
#include <sys/errno.h>
#include <libkern/libkern.h>
#include <pexpert/pexpert.h>
#include <machine/machine_routines.h>

#include "hfs_iokit.h"
#include "hfs.h"
#include "hfs_dbg.h"
#include "hfs_cnode.h"

#ifndef panic_on_assert
bool panic_on_assert;
#endif

#if DEBUG
bool hfs_corruption_panics = true;
#endif

extern struct vnodeopv_desc hfs_vnodeop_opv_desc;
extern struct vnodeopv_desc hfs_specop_opv_desc;
extern struct vnodeopv_desc hfs_fifoop_opv_desc;
extern struct vfsops hfs_vfsops;

void hfs_builtin_start(void);

void
hfs_builtin_start(void)
{
	static vfstable_t vfs_handle;
	struct vnodeopv_desc *op_descs[] = {
		&hfs_vnodeop_opv_desc,
		&hfs_specop_opv_desc,
#if FIFO
		&hfs_fifoop_opv_desc,
#endif
	};
#ifndef VFS_TBLVNOP_SECLUDE_RENAME
#define VFS_TBLVNOP_SECLUDE_RENAME 0
#endif
	struct vfs_fsentry vfe = {
		.vfe_vfsops = &hfs_vfsops,
		.vfe_vopcnt = sizeof(op_descs) / sizeof(op_descs[0]),
		.vfe_opvdescs = op_descs,
		.vfe_fsname = "hfs",
		.vfe_flags = (VFS_TBLNOTYPENUM | VFS_TBLLOCALVOL | VFS_TBLREADDIR_EXTENDED
		    | VFS_TBL64BITREADY | VFS_TBLVNOP_PAGEOUTV2 | VFS_TBLVNOP_PAGEINV2
		    | VFS_TBLTHREADSAFE | VFS_TBLCANMOUNTROOT | VFS_TBLVNOP_SECLUDE_RENAME
		    | VFS_TBLNATIVEXATTR)
	};
	int ret;

#ifndef panic_on_assert
	panic_on_assert = PE_i_can_has_kernel_configuration() & kPEICanHasAssertions;
#endif
	ret = vfs_fsadd(&vfe, &vfs_handle);
	if (ret) {
		printf("hfs: vfs_fsadd failed: %d\n", ret);
		return;
	}
	hfs_init_zones();
	/* (no hfs_sysctl_register(): built in, its nodes are registered from the kernel's sysctl set already) */
	{
		uint32_t cpus = (uint32_t)ml_get_max_cpu_number() + 1;

		if (2 * cpus > MAX_CACHED_ORIGINS_DEFAULT) {
			_hfs_max_origins = 2 * cpus;
			_hfs_max_file_origins = 2 * cpus;
		} else if (2 * cpus > MAX_CACHED_FILE_ORIGINS_DEFAULT) {
			_hfs_max_file_origins = 2 * cpus;
		}
	}
	printf("hfs: HFS+ registered (built in)\n");
}

int
hfs_is_ejectable(__unused const char *cdev_name)
{
	return 0;
}

void
hfs_iterate_media_with_content(__unused const char *content_uuid_cstring,
    __unused int (*func)(const char *bsd_name, const char *uuid_str, void *arg), __unused void *arg)
{
}

kern_return_t
hfs_get_platform_serial_number(__unused char *serial_number_str, __unused uint32_t len)
{
	return KERN_FAILURE;
}
