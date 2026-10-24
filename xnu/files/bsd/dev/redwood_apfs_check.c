/*
 * Mount the APFS volume on /dev/rvblk0 and walk it through VFS, so that what the driver
 * does can be compared with what tools/apfs/apfs.py reads from the same image on the
 * build machine.  Only with boot-arg apfs_check.
 *
 *   apfs_check=1              walk the whole volume and print only the totals
 *   apfs_check=2              also print a line per entry
 *   apfs_check=3              mount it and stop, leaving it for user space to read
 *                             (userland/src/apfs_mmap_test.c, which is the only way the
 *                             driver's vnop_pagein ever runs)
 *   apfs_check=4              mount nothing: walk the root filesystem, which is what the
 *                             root already is when the volume was handed to the kernel as
 *                             its root device.  This is how the phone is reached: it has
 *                             no driver for its own storage and no virtio, so the only
 *                             block device there is a RAM disk, and a RAM disk given to
 *                             the kernel as the root is mounted by vfs_mountroot.
 *   apfs_check=5              several threads walk the root filesystem at once, each over
 *                             the whole of it, to see what the one lock round apfs_lib
 *                             costs when there is more than one reader
 *   apfs_threads=N            how many, for mode 5 (default 4)
 *   apfs_check=6              mount apfs_dev on apfs_mnt, walk it on this thread and go
 *                             back to the loader.  For a volume that is not the root and
 *                             where there is no user space to make a mount point: the
 *                             point is a directory of the root image itself.
 *   apfs_dev=md1              the device to mount for modes 1 to 3 and 6 (default rvblk0)
 *   apfs_mnt=/lib             where to mount it for mode 6 (default /private/var/apfs-check)
 *   apfs_depth=N              go no deeper than N levels below where the walk starts
 *                             (1 = only the entries of that directory; 0 = no limit).
 *                             For splitting a volume too big to hold into parts: one part
 *                             takes the root's own entries, the others take a subtree
 *                             each, and the per-line hashes add up to the whole.
 *   apfs_max=N                stop after N entries (and say how memory stood on the way):
 *                             for finding out what a walk that never comes back runs into
 *   apfs_skip=/a,/b           directories not to go into, as paths inside the volume,
 *                             comma separated.  Their own line is still counted, only
 *                             their contents are left out -- which is how a volume too
 *                             big to hold is cut into parts that between them cover it
 *                             exactly once.
 *   apfs_subtree=/usr         walk only that part of the volume
 *   apfs_check=7              the one mode that writes: replace bytes inside a file of the
 *                             container on apfs_dev where they lie, with the kernel's own
 *                             bsd/miscfs/apfs/apfs_write.c, and read them back out of a
 *                             container opened again from nothing (apfs_write_check below)
 *   apfs_check=8              the store of fixed-length slot files on apfs_dev: save a payload
 *                             into it and read it back out of a container opened again from
 *                             nothing (apfs_store_check below)
 *
 * A line is the same one tools/apfs/apfs-tool's census and `verify.py clines` print:
 *
 *   path <TAB> d_type <TAB> mode <TAB> uid <TAB> gid <TAB> size <TAB> hash-of-contents
 *
 * The whole volume is 318098 entries, and printing them all through the kernel log would
 * be 47 MB down a serial line, so by default each line is folded into a sum instead: the
 * lines are hashed one at a time and the hashes added together.  Addition does not care
 * what order the entries came in, which matters because this walk and the one on the host
 * need not visit a directory's entries in the same order.  The host adds up the same
 * hashes over its own lines; if the two sums and counts agree, every path, kind, mode,
 * owner, group, size and byte of content agreed.  When they do not, apfs_check=2 over a
 * subtree gives lines to diff.
 *
 * Everything here goes through VFS -- vnode_lookup, vnode_getattr, VNOP_READDIR,
 * VNOP_READLINK, vn_rdwr -- so it exercises the vnode operations and not just the format
 * reading underneath them.
 */

#include <kern/thread.h>
#include <kern/thread_call.h>
#include <kern/clock.h>
#include <kern/kalloc.h>
#include <kern/locks.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <sys/dirent.h>
#include <sys/errno.h>
#include <sys/kauth.h>
#include <sys/param.h>
#include <sys/mount.h>
#include <sys/mount_internal.h>
#include <sys/namei.h>
#include <sys/proc.h>
#include <sys/stat.h>
#include <sys/systm.h>
#include <sys/uio.h>
#include <sys/uio_internal.h>
#include <sys/vnode.h>
#include <sys/vnode_if.h>
#include <sys/vnode_internal.h>
#include <sys/redwood_input.h>
#include <sys/sysctl.h>
#include <pexpert/pexpert.h>
#include <libkern/OSAtomic.h>
#include <sys/redwood_apfs_check.h>
#include <miscfs/apfs/apfs_lib.h>
#include <miscfs/apfs/apfs_write.h>
#include <miscfs/apfs/apfs_store.h>
#include <sys/disk.h>
#include <sys/buf.h>
#include <sys/fcntl.h>

extern void loader_return(void);        /* osfmk/arm64/loader_return.c */
extern void apfs_stats_print(void);     /* bsd/miscfs/apfs/apfs_vfsops.c */
extern void redwood_sparse_report(void);

/* how memory stands, for the progress line (osfmk/vm, bsd/vfs) */
extern unsigned int vm_page_free_count, vm_page_wire_count, memorystatus_available_pages;
extern long numvnodes;
extern int desiredvnodes;
#define CHECK_MB(pages) ((unsigned int)(((uint64_t)(pages) * PAGE_SIZE) >> 20))


/* ---- getting there ---- */

static int
make_mount_point(vfs_context_t ctx)
{
	struct componentname cn;
	struct vnode_attr va;
	vnode_t dvp = NULLVP, vp = NULLVP;
	char leaf[] = CHECK_LEAF;
	int error;

	error = vnode_lookup(CHECK_PARENT, 0, &dvp, ctx);
	if (error) {
		return error;
	}
	bzero(&cn, sizeof(cn));
	cn.cn_nameiop = CREATE;
	cn.cn_flags = ISLASTCN;
	cn.cn_context = ctx;
	cn.cn_pnbuf = leaf;
	cn.cn_pnlen = (int)sizeof(leaf);
	cn.cn_nameptr = leaf;
	cn.cn_namelen = (int)(sizeof(leaf) - 1);

	VATTR_INIT(&va);
	VATTR_SET(&va, va_type, VDIR);
	VATTR_SET(&va, va_mode, 0755);

	error = VNOP_MKDIR(dvp, &vp, &cn, &va, ctx);
	if (error == 0 && vp != NULLVP) {
		vnode_put(vp);
	}
	vnode_put(dvp);
	if (error == EEXIST) {
		error = 0;
	}
	return error;
}

#define MAX_THREADS 8

static volatile int apfs_check_running;

struct walk_job {
	int      index;
	uint64_t ms;
	uint64_t entries;
	uint64_t bytes;
	uint64_t sum;
	uint32_t errors;
};

static struct walk_job apfs_jobs[MAX_THREADS];

/* One reader: the whole root filesystem, with everything of its own. */
static void
apfs_walk_one(void *arg, __unused wait_result_t wr)
{
	struct walk_job *job = arg;
	struct census cs;
	uint64_t start, end, ns = 0, hash_ns = 0;

	bzero(&cs, sizeof(cs));
	cs.ctx = vfs_context_kernel();
	census_options(&cs);
	PE_parse_boot_argn("apfs_depth", &cs.max_depth, sizeof(cs.max_depth));
	PE_parse_boot_argn("apfs_max", &cs.max_entries, sizeof(cs.max_entries));
	{
		static char skip[256];

		if (PE_parse_boot_arg_str("apfs_skip", skip, sizeof(skip)) && skip[0] != '\0') {
			cs.skip = skip;
			printf("apfs-check: staying out of %s\n", skip);
		}
	}
	cs.lookup_flags = VNODE_LOOKUP_NOFOLLOW | VNODE_LOOKUP_NOCROSSMOUNT;
	cs.path = kalloc_data(PATH_CAP, Z_WAITOK | Z_ZERO);
	cs.buf = kalloc_data(READ_CHUNK, Z_WAITOK);
	if (cs.path == NULL || cs.buf == NULL) {
		printf("apfs-check: reader %d: out of memory\n", job->index);
		OSDecrementAtomic(&apfs_check_running);
		return;
	}
	start = mach_absolute_time();
	census_dir(&cs);
	end = mach_absolute_time();
	absolutetime_to_nanoseconds(end - start, &ns);
	absolutetime_to_nanoseconds(cs.hash_ticks, &hash_ns);

	job->ms = ns / 1000000;
	job->entries = cs.entries;
	job->bytes = cs.bytes;
	job->sum = cs.sum;
	job->errors = cs.errors;
	printf("apfs-check: reader %d: %llu entries  %llu bytes  sum %016llx  errors %u  "
	    "%llu ms  hash %s%llu ms\n", job->index, cs.entries, cs.bytes,
	    (unsigned long long)cs.sum, cs.errors, job->ms, cs.no_hash ? "off " : "",
	    hash_ns / 1000000);

	kfree_data(cs.path, PATH_CAP);
	kfree_data(cs.buf, READ_CHUNK);
	OSDecrementAtomic(&apfs_check_running);
}

/*
 * Several readers at once over the same volume.  What is wanted is not only that nothing
 * breaks but what the lock costs: all of apfs_lib runs under one mutex, so the part of the
 * work that is held time cannot go faster however many processors there are.
 */
static void
apfs_check_concurrent(void)
{
	uint32_t want = 4;
	uint64_t start, end, ns = 0;
	uint64_t total_bytes = 0, total_entries = 0;
	int i, n, bad = 0;

	if (PE_parse_boot_argn("apfs_threads", &want, sizeof(want)) == 0 || want == 0) {
		want = 4;
	}
	if (want > MAX_THREADS) {
		want = MAX_THREADS;
	}
	n = (int)want;
	printf("apfs-check: %d readers over the root filesystem at once\n", n);

	bzero(apfs_jobs, sizeof(apfs_jobs));
	apfs_check_running = n;
	start = mach_absolute_time();
	for (i = 0; i < n; i++) {
		thread_t th;

		apfs_jobs[i].index = i;
		if (kernel_thread_start(apfs_walk_one, &apfs_jobs[i], &th) != KERN_SUCCESS) {
			printf("apfs-check: cannot start reader %d\n", i);
			OSDecrementAtomic(&apfs_check_running);
			continue;
		}
		thread_deallocate(th);
	}
	while (apfs_check_running > 0) {
		delay_for_interval(50, NSEC_PER_MSEC);
	}
	end = mach_absolute_time();
	absolutetime_to_nanoseconds(end - start, &ns);

	for (i = 0; i < n; i++) {
		total_bytes += apfs_jobs[i].bytes;
		total_entries += apfs_jobs[i].entries;
		if (apfs_jobs[i].sum != apfs_jobs[0].sum ||
		    apfs_jobs[i].entries != apfs_jobs[0].entries || apfs_jobs[i].errors != 0) {
			bad++;
		}
	}
	printf("apfs-check: %d readers done in %llu ms\n", n, ns / 1000000);
	printf("apfs-check: together %llu entries  %llu bytes  %llu KB/s\n", total_entries,
	    total_bytes, ns ? (total_bytes * 1000000ull) / ns : 0);
	printf("apfs-check: readers that disagreed or failed: %d\n", bad);
	apfs_stats_print();
}

static void
apfs_check_thread(void *arg, __unused wait_result_t wr)
{
	uint32_t mode = (uint32_t)(uintptr_t)arg;
	vfs_context_t ctx = vfs_context_kernel();
	struct census cs;
	char devname[64] = CHECK_DEV;
	char subtree[256] = "";
	uint64_t start, end;
	int error, tries;

	bzero(&cs, sizeof(cs));
	cs.ctx = ctx;
	census_options(&cs);
	cs.print_lines = (mode == 2);
	/*
	 * NOCROSSMOUNT only where the volume is the root (modes 4 and 5).  In mode 6 the
	 * path leads from the root image into the mount, so crossing has to be allowed; the
	 * walk is kept on the volume by the check against the mount it started on.
	 */
	PE_parse_boot_argn("apfs_depth", &cs.max_depth, sizeof(cs.max_depth));
	PE_parse_boot_argn("apfs_max", &cs.max_entries, sizeof(cs.max_entries));
	{
		static char skip[256];

		if (PE_parse_boot_arg_str("apfs_skip", skip, sizeof(skip)) && skip[0] != '\0') {
			cs.skip = skip;
			printf("apfs-check: staying out of %s\n", skip);
		}
	}
	cs.lookup_flags = VNODE_LOOKUP_NOFOLLOW |
	    ((mode == 4 || mode == 5) ? VNODE_LOOKUP_NOCROSSMOUNT : 0);
	if (PE_parse_boot_arg_str("apfs_dev", subtree, sizeof(subtree)) && subtree[0] != '\0') {
		snprintf(devname, sizeof(devname), "/dev/%s", subtree);
	}
	subtree[0] = '\0';

	/* the modes that write: no walk, no mount, no census */
	if (mode == 7) {
		apfs_write_check(devname);
		return;
	}
	if (mode == 8) {
		apfs_store_check(devname);
		return;
	}

	cs.path = kalloc_data(PATH_CAP, Z_WAITOK | Z_ZERO);
	cs.buf = kalloc_data(READ_CHUNK, Z_WAITOK);
	if (cs.path == NULL || cs.buf == NULL) {
		printf("apfs-check: out of memory\n");
		return;
	}

	if (mode == 6) {
		char mnt[128] = CHECK_MNT;

		if (PE_parse_boot_arg_str("apfs_mnt", subtree, sizeof(subtree)) &&
		    subtree[0] == '/') {
			strlcpy(mnt, subtree, sizeof(mnt));
		}
		subtree[0] = '\0';
		/*
		 * A node just made with devfs_make_node() is not in /dev the instant it is
		 * made -- this runs straight after the device registered itself, and the first
		 * lookup comes back ENOENT -- so wait for it rather than guess how long devfs
		 * takes to show it.
		 */
		{
			vnode_t t = NULLVP;
			int tries;

			for (tries = 0; tries < 100; tries++) {
				if (vnode_lookup(devname, 0, &t, ctx) == 0) {
					vnode_put(t);
					break;
				}
				delay_for_interval(20, NSEC_PER_MSEC);
			}
			if (tries >= 100) {
				printf("apfs-check: %s never showed up in /dev\n", devname);
			} else if (tries > 0) {
				printf("apfs-check: %s showed up in /dev after %d ms\n", devname,
				    tries * 20);
			}
		}
		error = kernel_mount("apfs", NULLVP, NULLVP, mnt, devname, 0,
		    MNT_RDONLY | MNT_DONTBROWSE, KERNEL_MOUNT_NOAUTH, ctx);
		if (error) {
			printf("apfs-check: mounting %s on %s failed: %d\n", devname, mnt,
			    error);
			kfree_data(cs.path, PATH_CAP);
			kfree_data(cs.buf, READ_CHUNK);
			return;
		}
		printf("apfs-check: %s mounted on %s\n", devname, mnt);
		cs.rel = strlen(mnt);
		strlcpy(cs.path, mnt, PATH_CAP);
		cs.len = cs.rel;
		if (PE_parse_boot_arg_str("apfs_subtree", subtree, sizeof(subtree)) &&
		    subtree[0] == '/') {
			strlcat(cs.path, subtree, PATH_CAP);
			cs.len = strlen(cs.path);
			printf("apfs-check: walking %s\n", cs.path + cs.rel);
		} else {
			printf("apfs-check: walking the whole volume\n");
		}
		goto walk;
	}
	if (mode >= 5) {
		kfree_data(cs.path, PATH_CAP);
		kfree_data(cs.buf, READ_CHUNK);
		apfs_check_concurrent();
		return;
	}
	if (mode >= 4 && mode != 6) {
		/*
		 * The volume is already the root: nothing to mount, and no mount point to
		 * make, so none of what follows about /private/var applies.
		 */
		cs.rel = 0;
		cs.len = 0;
		cs.path[0] = '\0';
		printf("apfs-check: walking the root filesystem\n");
		goto walk;
	}

	/*
	 * /private/var is a ramfs that userland's init mounts, so the mount point cannot be
	 * made until that has happened.  Wait for it rather than guess how long it takes.
	 */
	for (tries = 0; tries < 180; tries++) {
		error = make_mount_point(ctx);
		if (error == 0) {
			break;
		}
		delay_for_interval(500, NSEC_PER_MSEC);
	}
	if (error) {
		printf("apfs-check: cannot make %s: %d\n", CHECK_MNT, error);
		return;
	}

	/*
	 * The mount data is the device's path itself, not a pointer to it: the extra
	 * indirection imageboot_pivot_image() uses is unpacked only on the BaseSystem path,
	 * which this build does not have, so apfs_mount() reads the string straight off
	 * this pointer.
	 */
	error = kernel_mount("apfs", NULLVP, NULLVP, CHECK_MNT, devname, 0,
	    MNT_RDONLY | MNT_DONTBROWSE, KERNEL_MOUNT_NOAUTH, ctx);
	if (error) {
		printf("apfs-check: mounting %s on %s failed: %d\n", devname, CHECK_MNT,
		    error);
		return;
	}
	printf("apfs-check: %s mounted on %s\n", devname, CHECK_MNT);
	if (mode >= 3) {
		/* someone in user space is going to read it; leave it alone */
		printf("apfs-check: left mounted, not walking it here\n");
		return;
	}

	cs.rel = sizeof(CHECK_MNT) - 1;
	strlcpy(cs.path, CHECK_MNT, PATH_CAP);
	cs.len = cs.rel;
	if (PE_parse_boot_arg_str("apfs_subtree", subtree, sizeof(subtree)) &&
	    subtree[0] == '/') {
		strlcat(cs.path, subtree, PATH_CAP);
		cs.len = strlen(cs.path);
		while (cs.len > cs.rel && cs.path[cs.len - 1] == '/') {
			cs.path[--cs.len] = '\0';
		}
		printf("apfs-check: walking %s\n", cs.path + cs.rel);
	} else {
		printf("apfs-check: walking the whole volume\n");
	}

walk:
	redwood_ufs_report("before the walk");
	start = mach_absolute_time();
	cs.t0 = start;
	watch_start(&cs);
	{
		uint32_t walkers = 1;

		PE_parse_boot_argn("apfs_walkers", &walkers, sizeof(walkers));
		if (mode == 6 && walkers > 1) {
			par_walk(&cs, walkers > PAR_MAX ? PAR_MAX : walkers);
		} else {
			uint64_t cpu0 = thread_get_runtime_self(), wall0 = mach_absolute_time();

			census_dir(&cs);
			cs.t_cpu = thread_get_runtime_self() - cpu0;
			cs.t_wall = mach_absolute_time() - wall0;
			census_times(&cs, 0);
		}
	}
	watch_stop();
	end = mach_absolute_time();

	{
		uint64_t ns = 0;

		absolutetime_to_nanoseconds(end - start, &ns);
		printf("apfs-check: done in %llu ms\n", ns / 1000000);
	}
	printf("apfs-check: entries %llu  dirs %llu  files %llu  symlinks %llu  other %llu\n",
	    cs.entries, cs.dirs, cs.files, cs.links, cs.others);
	printf("apfs-check: content bytes %llu\n", cs.bytes);
	printf("apfs-check: line-hash sum %016llx\n", (unsigned long long)cs.sum);
	printf("apfs-check: errors %u\n", cs.errors);
	{
		uint64_t hash_ns = 0;

		absolutetime_to_nanoseconds(cs.hash_ticks, &hash_ns);
		printf("apfs-check: of it the checker's own hashing %llu ms%s\n", hash_ns / 1000000,
		    cs.no_hash ? " (apfs_hash=0)" : "");
	}
	apfs_stats_print();
	redwood_sparse_report();
	redwood_ufs_report("after the walk");
	printf("apfs-check: compare with tools/apfs/verify.py sum\n");

	kfree_data(cs.path, PATH_CAP);
	kfree_data(cs.buf, READ_CHUNK);
}

static volatile int apfs_check_finished;

static void
apfs_check_runner(void *arg, wait_result_t wr)
{
	apfs_check_thread(arg, wr);
	apfs_check_finished = 1;
}

/*
 * sysctl kern.redwood_switch_root="INCOMING OUTGOING": the volume mounted on INCOMING
 * becomes the root, and what was the root until now is mounted on OUTGOING inside it (a
 * path without a leading slash, which must be a directory there -- on the iOS volume, a
 * synthetic one).  devfs stays on /dev (vfs_switch_root() keeps it there).  This is how
 * the iOS root volume on the phone's flash becomes the root: the kernel starts from the
 * small image, whose init mounts the volume, puts in place what the system needs on it
 * and then asks for this (userland/src/init.c, boot-arg root_apfs=).  Process 1 only, once.
 */
static int redwood_root_switched;

static int
redwood_sysctl_switch_root SYSCTL_HANDLER_ARGS
{
#pragma unused(oidp, arg1, arg2)
	char buf[512], *outgoing;
	int error;

	if (req->newptr == USER_ADDR_NULL) {
		return SYSCTL_OUT(req, redwood_root_switched ? "1" : "0", 2);
	}
	if (proc_getpid(current_proc()) != 1 || suser(kauth_cred_get(), NULL) != 0) {
		return EPERM;
	}
	if (redwood_root_switched) {
		return EBUSY;
	}
	if (req->newlen == 0 || req->newlen >= sizeof(buf)) {
		return EINVAL;
	}
	error = SYSCTL_IN(req, buf, req->newlen);
	if (error) {
		return error;
	}
	buf[req->newlen] = '\0';
	outgoing = strchr(buf, ' ');
	if (outgoing == NULL || buf[0] != '/') {
		return EINVAL;
	}
	*outgoing++ = '\0';
	error = vfs_switch_root(buf, outgoing, 0);
	printf("redwood: %s is the root now, the one before it on /%s: %d\n", buf, outgoing, error);
	if (error == 0) {
		redwood_root_switched = 1;
	}
	return error;
}

SYSCTL_PROC(_kern, OID_AUTO, redwood_switch_root, CTLTYPE_STRING | CTLFLAG_RW | CTLFLAG_LOCKED,
    NULL, 0, redwood_sysctl_switch_root, "A",
    "write \"INCOMING OUTGOING\": the volume on INCOMING becomes the root (process 1, once)");

/*
 * apfs_volume=DEV: a volume for user space in an ordinary boot.  Mounted read only (and
 * nosuid, nodev) on apfs_volume_at -- by default /System/Volumes/iOS, which the system image
 * has for it (userland/build.sh) -- right here, after the root and devfs and before launchd,
 * so that all of user space sees it from the start.  apfs_volume=ufs_darwin is the iOS root
 * container on the phone's flash (ufs=2 for the loader, bootcpu=7).
 */
static void
apfs_volume_mount(void)
{
	char dev[48], at[256] = "/System/Volumes/iOS", path[64];
	vfs_context_t ctx = vfs_context_kernel();
	uint64_t t0, ns = 0;
	vnode_t vp = NULLVP;
	int error, tries;

	if (!PE_parse_boot_arg_str("apfs_volume", dev, sizeof(dev)) || dev[0] == '\0') {
		return;
	}
	(void)PE_parse_boot_arg_str("apfs_volume_at", at, sizeof(at));
	snprintf(path, sizeof(path), "/dev/%s", dev);
	/* a node devfs_make_node() has just made is not in /dev at once (see apfs_check_thread) */
	for (tries = 0; tries < 100; tries++) {
		if (vnode_lookup(path, 0, &vp, ctx) == 0) {
			vnode_put(vp);
			break;
		}
		delay_for_interval(50, NSEC_PER_MSEC);
	}
	t0 = mach_absolute_time();
	error = kernel_mount("apfs", NULLVP, NULLVP, at, path, 0,
	    MNT_RDONLY | MNT_NOSUID | MNT_NODEV, KERNEL_MOUNT_NOAUTH, ctx);
	absolutetime_to_nanoseconds(mach_absolute_time() - t0, &ns);
	if (error) {
		printf("apfs: mounting %s on %s failed: %d\n", path, at, error);
	} else {
		printf("apfs: %s mounted on %s, read only, in %llu ms\n", path, at, ns / 1000000);
	}
}

void
redwood_apfs_check_init(void)
{
	uint32_t mode = 0;
	thread_t th;

	apfs_volume_mount();

	if (!PE_parse_boot_argn("apfs_check", &mode, sizeof(mode)) || mode == 0) {
		return;
	}
	if (mode >= 4) {
		/*
		 * The volume is the root (or mode 6, which mounts it here), and bsd_init is
		 * about to exec /sbin/launchd off it, which on anything but an image of ours
		 * (stage0 as /sbin/launchd) ends in a panic.  So the walk has to be over before this returns.
		 *
		 * It does not run on this thread, though.  This is the boot thread: it runs
		 * above the threads that service thread calls, and a walk off a RAM disk never
		 * blocks, so for as long as it lasts no thread call runs at all -- not even
		 * the one that feeds the phone's watchdog every ten seconds.  On the phone a
		 * walk past half a minute was reset with no log to say why; in QEMU, which
		 * has no such watchdog, the same walk let not one 6-second timer fire in 72 s.
		 * So the walk gets a thread of ordinary priority, and this one sleeps until
		 * it is done.
		 */
		thread_t th;

		apfs_check_finished = 0;
		if (kernel_thread_start(apfs_check_runner, (void *)(uintptr_t)mode, &th) !=
		    KERN_SUCCESS) {
			printf("apfs-check: cannot start the thread\n");
			return;
		}
		thread_deallocate(th);
		while (!apfs_check_finished) {
			delay_for_interval(100, NSEC_PER_MSEC);     /* sleeps */
		}
		printf("apfs-check: going back to the loader\n");
		loader_return();
		return;
	}
	if (kernel_thread_start(apfs_check_thread, (void *)(uintptr_t)mode, &th) !=
	    KERN_SUCCESS) {
		printf("apfs-check: cannot start the thread\n");
		return;
	}
	thread_deallocate(th);
}
