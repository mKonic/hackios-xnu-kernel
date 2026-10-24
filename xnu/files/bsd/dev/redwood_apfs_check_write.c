/*
 * apfs_check=7 and =8: the kernel writes and reads back a volume through its own
 * apfs_lib and store code, on a device.  redwood_apfs_check.c decides when to come
 * here; what is shared sits in <sys/redwood_apfs_check.h>.
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

/* ------------------------------------------- apfs_check=7: the kernel's own write, on a device */

/*
 * The one place bsd/miscfs/apfs/apfs_write.c is run inside the kernel.  It is not VFS: there is
 * no vnop_write and this milestone does not call for one.  It is apfs_lib and apfs_write on a
 * real block device, which is the step between the tests on the build machine
 * (tools/apfs/write-tests.sh) and anything on the phone's flash.
 *
 *   apfs_check=7
 *   apfs_dev=md0              the container to write in (default rvblk0)
 *   apfs_write_path=/redwood/x   the file in it, by path from the volume's root
 *   apfs_write_at=N           where in the file (default 0)
 *   apfs_write_len=N          how many bytes (default 4096, at most APFS_WRITE_MAX)
 *   apfs_write_byte=N         what to fill them with (default 0xa5)
 *
 * The container is opened, written and closed; then opened again from nothing and the file read
 * back, so what is reported came off the device and not out of apfs_lib's block cache.  The
 * device is opened for writing here and nowhere else: every mount of this driver leaves
 * dev.write NULL, and without it apfs_write_in_place refuses everything.
 *
 * Where this can reach, which is the point: a block device.  The phone's flash is not one under
 * this kernel -- it is read through the loader's mailbox and, on the branch that writes to it at
 * all, through a character device of its own with its own bounds (bsd/dev/redwood_ufs.c) -- so
 * nothing here can touch it.  In QEMU and on the phone alike the only block devices are RAM
 * disks.  And it happens at all only when asked for twice over: apfs_check=7 and a path.
 */
#define APFS_WRITE_MAX (1u << 20)

struct wdev {
	vnode_t  vp;
	uint32_t devblock;
};

static int
wdev_io(struct wdev *w, uint64_t off, void *buf, size_t len, int flag)
{
	char *p = buf;

	if ((off % w->devblock) != 0 || (len % w->devblock) != 0) {
		return EINVAL;
	}
	while (len > 0) {
		size_t n = len < (128u << 10) ? len : (128u << 10);
		buf_t bp = buf_alloc(w->vp);
		int error;

		if (bp == NULL) {
			return ENOMEM;
		}
		buf_reset(bp, flag);
		buf_setblkno(bp, (daddr64_t)(off / w->devblock));
		buf_setlblkno(bp, (daddr64_t)(off / w->devblock));
		buf_setcount(bp, (uint32_t)n);
		buf_setdataptr(bp, (uintptr_t)p);
		/*
		 * A write buffer of our own has to be counted against the device's vnode before
		 * it is issued: buf_biodone() undoes that count, and without it the kernel
		 * panics on the way back (vnode_writedone: numoutput < 0).  This is what
		 * cluster_io() does for the same kind of raw buffer.
		 */
		if (flag != B_READ) {
			vnode_startwrite(w->vp);
		}
		error = VNOP_STRATEGY(bp);
		if (error == 0) {
			error = (int)buf_biowait(bp);
		}
		if (error == 0 && (buf_resid(bp) != 0 || buf_count(bp) != n)) {
			error = EIO;
		}
		buf_free(bp);
		if (error) {
			return error;
		}
		p += n;
		off += n;
		len -= n;
	}
	return 0;
}

static int
wdev_read(void *ctx, uint64_t off, void *buf, size_t len)
{
	return wdev_io(ctx, off, buf, len, B_READ);
}

static int
wdev_write(void *ctx, uint64_t off, const void *buf, size_t len)
{
	return wdev_io(ctx, off, (void *)(uintptr_t)buf, len, B_WRITE);
}

/* The volume a path names, and the inode in it; the first volume with a root directory. */
static int
wdev_open_volume(struct apfs_nx *nx, struct apfs_vol *vol)
{
	uint32_t i;

	for (i = 0; i < APFS_NX_MAX_FILE_SYSTEMS; i++) {
		struct apfs_inode ino;

		if (nx->fs_oid[i] == 0 || apfs_vol_open(nx, i, vol) != 0) {
			continue;
		}
		if (apfs_inode_get(vol, APFS_ROOT_DIR_INO_NUM, &ino) == 0) {
			return 0;
		}
	}
	return ENOENT;
}

/* The FNV-1a of a file's contents as the census lines use it, read through apfs_lib. */
static int
wdev_hash(struct apfs_vol *vol, const struct apfs_inode *ino, uint8_t *buf, size_t buflen,
    uint64_t *out)
{
	struct apfs_cfile cf;
	uint64_t h = FNV_INIT, off = 0;

	bzero(&cf, sizeof(cf));
	while (off < ino->size) {
		size_t want = (size_t)(ino->size - off), got = 0, i;

		if (want > buflen) {
			want = buflen;
		}
		if (apfs_read_file(vol, ino, &cf, off, buf, want, &got) != 0 || got == 0) {
			apfs_cfile_fini(&cf);
			return EIO;
		}
		for (i = 0; i < got; i++) {
			h ^= buf[i];
			h *= FNV_PRIME;
		}
		off += got;
	}
	apfs_cfile_fini(&cf);
	*out = h;
	return 0;
}

void
apfs_write_check(const char *devname)
{
	vfs_context_t ctx = vfs_context_kernel();
	struct apfs_nx nx;
	struct apfs_vol vol;
	struct apfs_inode ino;
	struct apfs_write_report rep;
	struct apfs_dev dev;
	struct wdev w = { NULLVP, 0 };
	char path[256] = "";
	uint8_t *data = NULL, *scratch = NULL, *was = NULL;
	uint64_t at = 0, before = 0, after = 0, want = 0, file_id = 0, was_len = 0;
	uint32_t len = 4096, byte = 0xa5;
	const char *why = NULL;
	bool opened = false, nx_open = false;
	int error, tries;

	if (!PE_parse_boot_arg_str("apfs_write_path", path, sizeof(path)) || path[0] != '/') {
		printf("apfs-write: apfs_write_path=/some/file says which file to write\n");
		return;
	}
	PE_parse_boot_argn("apfs_write_at", &at, sizeof(at));
	PE_parse_boot_argn("apfs_write_len", &len, sizeof(len));
	PE_parse_boot_argn("apfs_write_byte", &byte, sizeof(byte));
	if (len == 0 || len > APFS_WRITE_MAX) {
		printf("apfs-write: apfs_write_len=%u is not between 1 and %u\n", len, APFS_WRITE_MAX);
		return;
	}

	for (tries = 0; tries < 100; tries++) {
		if (vnode_lookup(devname, 0, &w.vp, ctx) == 0) {
			break;
		}
		delay_for_interval(20, NSEC_PER_MSEC);
	}
	if (w.vp == NULLVP) {
		printf("apfs-write: %s never showed up in /dev\n", devname);
		return;
	}
	/* for writing, which is what no mount of this driver ever asks for */
	error = VNOP_OPEN(w.vp, FREAD | FWRITE, ctx);
	if (error) {
		printf("apfs-write: %s will not open for writing: %d\n", devname, error);
		goto out;
	}
	opened = true;
	if (VNOP_IOCTL(w.vp, DKIOCGETBLOCKSIZE, (caddr_t)&w.devblock, 0, ctx) != 0 ||
	    w.devblock == 0 || (4096 % w.devblock) != 0) {
		printf("apfs-write: %s has no usable block size\n", devname);
		goto out;
	}
	data = kalloc_data(len, Z_WAITOK);
	scratch = kalloc_data(READ_CHUNK, Z_WAITOK);
	if (data == NULL || scratch == NULL) {
		printf("apfs-write: out of memory\n");
		goto out;
	}
	memset(data, (int)(byte & 0xff), len);

	memset(&dev, 0, sizeof(dev));
	dev.ctx = &w;
	dev.read = wdev_read;
	dev.read_data = wdev_read;
	dev.write = wdev_write;
	error = apfs_nx_open(&nx, &dev);
	if (error) {
		printf("apfs-write: %s is not a container we can read: %d\n", devname, error);
		goto out;
	}
	nx_open = true;
	error = wdev_open_volume(&nx, &vol);
	if (error) {
		printf("apfs-write: %s has no volume with a root directory\n", devname);
		goto out;
	}
	if (apfs_write_volume_ok(&vol, &why) != 0) {
		printf("apfs-write: volume %s will not be written: %s\n", vol.name, why);
		goto out;
	}
	error = apfs_path_lookup(&vol, path, &file_id, NULL);
	if (error) {
		printf("apfs-write: %s: no such path in %s: %d\n", path, vol.name, error);
		goto out;
	}
	error = apfs_inode_get(&vol, file_id, &ino);
	if (error == 0) {
		error = wdev_hash(&vol, &ino, scratch, READ_CHUNK, &before);
	}
	if (error) {
		printf("apfs-write: %s cannot be read before the write: %d\n", path, error);
		goto out;
	}
	printf("apfs-write: %s on %s, inode %llu, %llu bytes, hash %016llx\n", path, vol.name,
	    file_id, ino.size, before);
	/*
	 * The file as it is now, kept so that what it should hold afterwards can be worked out
	 * from it rather than from the result -- which would prove nothing.
	 */
	if (ino.size <= APFS_WRITE_MAX) {
		struct apfs_cfile cf;
		size_t got = 0;

		bzero(&cf, sizeof(cf));
		was_len = ino.size;
		was = kalloc_data((vm_size_t)was_len, Z_WAITOK);
		if (was != NULL &&
		    (apfs_read_file(&vol, &ino, &cf, 0, was, (size_t)was_len, &got) != 0 ||
		    got != was_len)) {
			kfree_data(was, (vm_size_t)was_len);
			was = NULL;
		}
		apfs_cfile_fini(&cf);
	}
	if (was == NULL) {
		printf("apfs-write: %s is too big to keep a copy of: only the hashes are reported\n",
		    path);
	}

	error = apfs_write_in_place(&vol, file_id, at, data, len, &rep);
	printf("apfs-write: %u bytes of 0x%02x at %llu: %s; %llu blocks from %llu, %llu of them "
	    "patched whole, %llu writes\n", len, byte & 0xff, at,
	    error == 0 ? "written" : (rep.why != NULL ? rep.why : "failed"), rep.blocks,
	    rep.first_block, rep.partial, rep.calls);
	apfs_nx_close(&nx);
	nx_open = false;
	if (error) {
		goto out;
	}

	/*
	 * Again from nothing: a container opened a second time shares no cache with the first,
	 * so what comes back now is what is on the device.
	 */
	error = apfs_nx_open(&nx, &dev);
	if (error == 0) {
		nx_open = true;
		error = wdev_open_volume(&nx, &vol);
	}
	if (error == 0) {
		error = apfs_path_lookup(&vol, path, &file_id, NULL);
	}
	if (error == 0) {
		error = apfs_inode_get(&vol, file_id, &ino);
	}
	if (error == 0) {
		error = wdev_hash(&vol, &ino, scratch, READ_CHUNK, &after);
	}
	if (error) {
		printf("apfs-write: the volume will not read back after the write: %d\n", error);
		goto out;
	}
	if (was != NULL) {
		uint64_t off;

		want = FNV_INIT;
		for (off = 0; off < was_len; off++) {
			uint8_t b = (off >= at && off < at + len) ? (uint8_t)(byte & 0xff) : was[off];

			want ^= b;
			want *= FNV_PRIME;
		}
	}
	printf("apfs-write: read back from a container opened again: %llu bytes, hash %016llx "
	    "(was %016llx); the bytes written are %s\n", ino.size, after, before,
	    was == NULL ? "not checked here" :
	    (want == after ? "all there and nothing else changed" : "NOT what was asked for"));
out:
	if (nx_open) {
		apfs_nx_close(&nx);
	}
	if (data != NULL) {
		kfree_data(data, len);
	}
	if (scratch != NULL) {
		kfree_data(scratch, READ_CHUNK);
	}
	if (was != NULL) {
		kfree_data(was, (vm_size_t)was_len);
	}
	if (opened) {
		(void)VNOP_CLOSE(w.vp, FREAD | FWRITE, ctx);
	}
	if (w.vp != NULLVP) {
		vnode_put(w.vp);
	}
}

/* -------------------------------------- apfs_check=8: the store, in the kernel, on a device */

/*
 * The store of fixed-length slot files (bsd/miscfs/apfs/apfs_store.c) run where it will live: in
 * the kernel, on a block device, under the kernel's own allocator.  A payload of apfs_store_len
 * bytes of a pattern this works out twice -- once to save, once to compare -- goes into the
 * store on apfs_dev; then the container is opened again from nothing and the newest good slot is
 * read back, so what is reported came off the device.
 *
 *   apfs_check=8
 *   apfs_dev=md1              the store volume (default rvblk0); its name must be RedwoodStore
 *   apfs_store_len=N          how many bytes of payload (default 40000)
 *   apfs_store_seed=N         which pattern (default 1); a different seed is a different payload
 *   apfs_store_note=N         the writer's own number, kept in the header (default 0)
 */
#define APFS_STORE_TEST_MAX (4u << 20)

static void
store_pattern(uint8_t *buf, uint32_t len, uint32_t seed)
{
	uint32_t i;

	for (i = 0; i < len; i++) {
		buf[i] = (uint8_t)(i * (7 + seed) + 5 + seed);
	}
}

void
apfs_store_check(const char *devname)
{
	vfs_context_t ctx = vfs_context_kernel();
	struct apfs_nx nx;
	struct apfs_vol vol;
	struct apfs_store st;
	struct apfs_store_head head;
	struct apfs_store_write w;
	struct apfs_dev dev;
	struct wdev wd = { NULLVP, 0 };
	uint8_t *data = NULL, *scratch = NULL;
	uint32_t len = 40000, seed = 1;
	uint64_t note = 0, want = FNV_INIT;
	const char *why = NULL;
	bool opened = false, nx_open = false;
	int best, error, tries;
	uint32_t i;

	PE_parse_boot_argn("apfs_store_len", &len, sizeof(len));
	PE_parse_boot_argn("apfs_store_seed", &seed, sizeof(seed));
	PE_parse_boot_argn("apfs_store_note", &note, sizeof(note));
	if (len == 0 || len > APFS_STORE_TEST_MAX) {
		printf("apfs-store: apfs_store_len=%u is not between 1 and %u\n", len,
		    APFS_STORE_TEST_MAX);
		return;
	}
	for (tries = 0; tries < 100; tries++) {
		if (vnode_lookup(devname, 0, &wd.vp, ctx) == 0) {
			break;
		}
		delay_for_interval(20, NSEC_PER_MSEC);
	}
	if (wd.vp == NULLVP) {
		printf("apfs-store: %s never showed up in /dev\n", devname);
		return;
	}
	error = VNOP_OPEN(wd.vp, FREAD | FWRITE, ctx);
	if (error) {
		printf("apfs-store: %s will not open for writing: %d\n", devname, error);
		goto out;
	}
	opened = true;
	if (VNOP_IOCTL(wd.vp, DKIOCGETBLOCKSIZE, (caddr_t)&wd.devblock, 0, ctx) != 0 ||
	    wd.devblock == 0 || (4096 % wd.devblock) != 0) {
		printf("apfs-store: %s has no usable block size\n", devname);
		goto out;
	}
	data = kalloc_data(len, Z_WAITOK);
	scratch = kalloc_data(READ_CHUNK, Z_WAITOK);
	if (data == NULL || scratch == NULL) {
		printf("apfs-store: out of memory\n");
		goto out;
	}
	store_pattern(data, len, seed);
	for (i = 0; i < len; i++) {
		want ^= data[i];
		want *= FNV_PRIME;
	}

	memset(&dev, 0, sizeof(dev));
	dev.ctx = &wd;
	dev.read = wdev_read;
	dev.read_data = wdev_read;
	dev.write = wdev_write;
	error = apfs_nx_open(&nx, &dev);
	if (error) {
		printf("apfs-store: %s is not a container we can read: %d\n", devname, error);
		goto out;
	}
	nx_open = true;
	error = wdev_open_volume(&nx, &vol);
	if (error == 0) {
		error = apfs_store_open(&vol, &st, &why);
	}
	if (error) {
		printf("apfs-store: %s holds no store: %s\n", devname,
		    why != NULL ? why : "cannot be opened");
		goto out;
	}
	best = apfs_store_newest(&st, scratch, READ_CHUNK, &head);
	printf("apfs-store: %s: %d slots of %llu bytes, %llu of payload; the newest good slot is "
	    "%d%s\n", vol.name, (int)APFS_STORE_SLOTS, st.slot_bytes, st.payload_max, best,
	    best < 0 ? " (none)" : "");

	error = apfs_store_save(&st, data, len, 0, note, scratch, READ_CHUNK, &w);
	printf("apfs-store: %u bytes into slot %d as generation %llu: %s; %llu blocks\n", len,
	    w.slot, w.generation, error == 0 ? "saved" : "failed", w.blocks);
	apfs_nx_close(&nx);
	nx_open = false;
	if (error) {
		printf("apfs-store: the save failed: %d\n", error);
		goto out;
	}

	/* again from nothing: no cache of the first opening survives into the second */
	error = apfs_nx_open(&nx, &dev);
	if (error == 0) {
		nx_open = true;
		error = wdev_open_volume(&nx, &vol);
	}
	if (error == 0) {
		error = apfs_store_open(&vol, &st, &why);
	}
	if (error) {
		printf("apfs-store: the store will not open again: %d\n", error);
		goto out;
	}
	best = apfs_store_newest(&st, scratch, READ_CHUNK, &head);
	if (best < 0) {
		printf("apfs-store: after the save no slot holds anything good\n");
		goto out;
	}
	printf("apfs-store: read back from a container opened again: slot %d, generation %llu, "
	    "%llu bytes, note %llu, sum %016llx; the payload is %s\n", best, head.generation,
	    head.bytes, head.note, head.sum,
	    (head.bytes == len && head.sum == want) ? "the one that was saved" :
	    "NOT what was saved");
out:
	if (nx_open) {
		apfs_nx_close(&nx);
	}
	if (data != NULL) {
		kfree_data(data, len);
	}
	if (scratch != NULL) {
		kfree_data(scratch, READ_CHUNK);
	}
	if (opened) {
		(void)VNOP_CLOSE(wd.vp, FREAD | FWRITE, ctx);
	}
	if (wd.vp != NULLVP) {
		vnode_put(wd.vp);
	}
}
