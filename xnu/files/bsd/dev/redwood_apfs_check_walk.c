/*
 * Walking the mounted volume: the census, its hash and progress lines, the stuck
 * detector and the parallel walkers.  Entry points and what to compare against live
 * in redwood_apfs_check.c; the shared bits sit in <sys/redwood_apfs_check.h>.
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

/* which operation the walk is in, for the stuck detector further down */
static const char *volatile watch_phase = "";
#define PHASE(name) (watch_phase = (name))

#define DIR_CHUNK  4096
#define MAX_DEPTH  48

static uint64_t
fnv1a(const uint8_t *p, size_t n, uint64_t h)
{
	size_t i;

	for (i = 0; i < n; i++) {
		h ^= p[i];
		h *= FNV_PRIME;
	}
	return h;
}

/* The boot-args both kinds of walk take the same way. */
void
census_options(struct census *cs)
{
	uint32_t hash = 1, contents = 1;

	PE_parse_boot_argn("apfs_hash", &hash, sizeof(hash));
	PE_parse_boot_argn("apfs_contents", &contents, sizeof(contents));
	cs->no_hash = (hash == 0);
	cs->no_contents = (contents == 0);
}

/* The format bits of a mode, which vnode_getattr does not report in va_mode. */
static uint16_t
ifmt_of(enum vtype t)
{
	switch (t) {
	case VREG:  return S_IFREG;
	case VDIR:  return S_IFDIR;
	case VLNK:  return S_IFLNK;
	case VCHR:  return S_IFCHR;
	case VBLK:  return S_IFBLK;
	case VFIFO: return S_IFIFO;
	case VSOCK: return S_IFSOCK;
	default:    return 0;
	}
}

/* FNV-1a over a regular file's contents, read through VNOP_READ. */
static int
hash_file(struct census *cs, vnode_t vp, uint64_t size, uint64_t *out)
{
	uint64_t h = FNV_INIT, off = 0;

	while (off < size) {
		int want = (int)((size - off > READ_CHUNK) ? READ_CHUNK : (size - off));
		int resid = 0;
		int error = vn_rdwr(UIO_READ, vp, (caddr_t)cs->buf, want, (off_t)off,
		    UIO_SYSSPACE, IO_NOCACHE, vfs_context_ucred(cs->ctx), &resid,
		    vfs_context_proc(cs->ctx));

		if (error) {
			return error;
		}
		if (resid == want) {
			break;                  /* nothing more to be had */
		}
		if (!cs->no_hash) {
			uint64_t t0 = mach_absolute_time();

			h = fnv1a(cs->buf, (size_t)(want - resid), h);
			cs->hash_ticks += mach_absolute_time() - t0;
		}
		cs->bytes += (uint64_t)(want - resid);
		off += (uint64_t)(want - resid);
	}
	*out = h;
	return 0;
}

/* FNV-1a over a symbolic link's target, read through VNOP_READLINK. */
static int
hash_link(struct census *cs, vnode_t vp, uint64_t *out)
{
	uio_t uio;
	char uio_buf[UIO_SIZEOF(1)];
	int error;
	size_t n;

	uio = uio_createwithbuffer(1, 0, UIO_SYSSPACE, UIO_READ, uio_buf, sizeof(uio_buf));
	if (uio == NULL) {
		return ENOMEM;
	}
	uio_addiov(uio, CAST_USER_ADDR_T(cs->buf), MAXPATHLEN);
	error = VNOP_READLINK(vp, uio, cs->ctx);
	if (error) {
		return error;
	}
	n = MAXPATHLEN - (size_t)uio_resid(uio);
	cs->bytes += n;
	*out = fnv1a(cs->buf, n, FNV_INIT);
	return 0;
}

static bool
census_entry(struct census *cs, uint8_t d_type)
{
	struct vnode_attr va;
	vnode_t vp = NULLVP;
	uint64_t h = 0, size = 0;
	uint16_t mode = 0;
	char line[PATH_CAP + 128];
	int error;
	int n;

	/*
	 * Do not follow a symbolic link: the point is to look at the link itself, as the host
	 * reader does.  Following them would report the target's kind, size and contents
	 * instead, and a link whose target is missing -- of which this volume has several --
	 * would look like a lookup failure.  Crossing mount points has to stay allowed: the
	 * path leads from the ramfs that holds the mount point into the volume itself.
	 */
	PHASE("lookup");
	{
		uint64_t t0 = mach_absolute_time();

		error = vnode_lookup(cs->path, cs->lookup_flags, &vp, cs->ctx);
		cs->t_lookup += mach_absolute_time() - t0;
	}
	if (error) {
		printf("apfs-check: %s: lookup failed: %d\n", cs->path + cs->rel, error);
		cs->errors++;
		return false;
	}
	if (cs->mp != NULL && vnode_mount(vp) != cs->mp) {
		vnode_put(vp);          /* another filesystem is mounted here */
		return false;
	}
	VATTR_INIT(&va);
	VATTR_WANTED(&va, va_mode);
	VATTR_WANTED(&va, va_uid);
	VATTR_WANTED(&va, va_gid);
	VATTR_WANTED(&va, va_data_size);
	PHASE("getattr");
	{
		uint64_t t0 = mach_absolute_time();

		error = vnode_getattr(vp, &va, cs->ctx);
		cs->t_attr += mach_absolute_time() - t0;
	}
	if (error) {
		printf("apfs-check: %s: getattr failed: %d\n", cs->path + cs->rel, error);
		cs->errors++;
		goto out;
	}
	mode = (uint16_t)(ifmt_of(vnode_vtype(vp)) | (VATTR_IS_SUPPORTED(&va, va_mode)
	    ? (va.va_mode & 07777) : 0));
	size = VATTR_IS_SUPPORTED(&va, va_data_size) ? va.va_data_size : 0;

	if (vnode_vtype(vp) == VREG) {
		cs->files++;
		PHASE("read");
		if (!cs->no_contents) {
			uint64_t t0 = mach_absolute_time();

			error = hash_file(cs, vp, size, &h);
			cs->t_read += mach_absolute_time() - t0;
		}
	} else if (vnode_vtype(vp) == VLNK) {
		cs->links++;
		PHASE("readlink");
		if (!cs->no_contents) {
			uint64_t t0 = mach_absolute_time();

			error = hash_link(cs, vp, &h);
			cs->t_read += mach_absolute_time() - t0;
		}
	} else if (vnode_vtype(vp) == VDIR) {
		cs->dirs++;
	} else {
		cs->others++;
	}
	if (error) {
		printf("apfs-check: %s: reading it failed: %d\n", cs->path + cs->rel, error);
		cs->errors++;
		goto out;
	}

	n = scnprintf(line, sizeof(line), "%s\t%u\t%06o\t%u\t%u\t%llu\t%016llx\n",
	    cs->path + cs->rel, d_type, mode,
	    VATTR_IS_SUPPORTED(&va, va_uid) ? va.va_uid : 0,
	    VATTR_IS_SUPPORTED(&va, va_gid) ? va.va_gid : 0,
	    (unsigned long long)size, (unsigned long long)h);
	cs->sum += fnv1a((const uint8_t *)line, (size_t)n, FNV_INIT);
	cs->entries++;
	if (cs->print_lines) {
		printf("apfs-line: %s", line);
	}
	if ((cs->entries % 10000) == 0) {
		uint64_t ns = 0;

		absolutetime_to_nanoseconds(mach_absolute_time() - cs->t0, &ns);
		printf("apfs-check: %llu entries so far at %llu ms  free %u MB  wired %u MB  "
		    "available %u MB  vnodes %ld of %d\n", cs->entries, ns / 1000000,
		    CHECK_MB(vm_page_free_count),
		    CHECK_MB(vm_page_wire_count), CHECK_MB(memorystatus_available_pages),
		    numvnodes, desiredvnodes);
	}
	if (cs->max_entries != 0 && cs->entries >= cs->max_entries) {
		cs->stop = true;
	}
out:
	PHASE("vnode_put");
	vnode_put(vp);
	PHASE("");
	return error == 0;
}

void census_dir(struct census *cs);
static void par_push(const char *path, size_t len, int depth);

/* the walkers of a parallel walk, for the watch's count of progress (PAR_MAX in this header) */
static struct census *volatile par_cs[PAR_MAX];
static volatile uint32_t par_n;

/*
 * A walk that stops making progress, rather than one that fails.  On the phone a walk of
 * the real volume can stop dead past some tens of thousands of entries, and then nothing
 * is left to say why: the watchdog resets the phone and the log goes with it.  So a timer
 * looks every few seconds; if the count of entries has not moved, it says which entry and
 * which operation the walk is in, and goes back to the loader -- with the log -- before
 * the watchdog does.
 */
#define WATCH_SECONDS 6

static struct census *volatile watch_cs;
static uint64_t watch_last = ~0ULL;
static thread_call_t watch_call;

static void
watch_tick(__unused thread_call_param_t p0, __unused thread_call_param_t p1)
{
	struct census *cs = watch_cs;
	uint64_t deadline, progress;

	if (cs == NULL) {
		return;                 /* the walk is over */
	}
	/* entries and bytes both count as progress: one big file can take a while */
	progress = cs->entries + cs->bytes;
	for (uint32_t i = 0; i < par_n; i++) {
		if (par_cs[i] != NULL) {
			progress += par_cs[i]->entries + par_cs[i]->bytes;
		}
	}
	if (progress == watch_last) {
		printf("apfs-check: STUCK: no progress for %d s after entry %llu, at %s, in %s\n",
		    WATCH_SECONDS, cs->entries, cs->path + cs->rel, watch_phase);
		apfs_stats_print();
		redwood_sparse_report();
		printf("apfs-check: going back to the loader with that\n");
		loader_return();
		return;
	}
	/* a sign the timer got to run at all: a walk can starve every thread call */
	printf("apfs-check: tick, %llu entries\n", cs->entries);
	watch_last = progress;
	clock_interval_to_deadline(WATCH_SECONDS, NSEC_PER_SEC, &deadline);
	thread_call_enter_delayed(watch_call, deadline);
}

void
watch_start(struct census *cs)
{
	uint64_t deadline;

	if (watch_call == NULL) {
		watch_call = thread_call_allocate(watch_tick, NULL);
	}
	watch_last = ~0ULL;
	watch_cs = cs;
	clock_interval_to_deadline(WATCH_SECONDS, NSEC_PER_SEC, &deadline);
	thread_call_enter_delayed(watch_call, deadline);
}

void
watch_stop(void)
{
	watch_cs = NULL;
	if (watch_call != NULL) {
		thread_call_cancel(watch_call);
	}
}

/* Is this directory one of the ones to stay out of?  Compares the path inside the volume. */
static bool
census_skipped(struct census *cs)
{
	const char *want = cs->path + cs->rel;
	size_t want_len = strlen(want);
	const char *at = cs->skip;

	if (at == NULL) {
		return false;
	}
	while (*at != '\0') {
		const char *end = strchr(at, ',');
		size_t n = (end != NULL) ? (size_t)(end - at) : strlen(at);

		if (n == want_len && memcmp(at, want, n) == 0) {
			return true;
		}
		if (end == NULL) {
			break;
		}
		at = end + 1;
	}
	return false;
}

/* Add "/name" to the path, do the entry, and put the path back as it was. */
static void
census_child(struct census *cs, const char *name, size_t name_len, uint8_t d_type)
{
	size_t saved = cs->len;

	if (cs->stop) {
		return;
	}
	if (cs->len + name_len + 2 > PATH_CAP) {
		printf("apfs-check: path below %s is too long\n", cs->path + cs->rel);
		cs->errors++;
		return;
	}
	cs->path[cs->len++] = '/';
	memcpy(cs->path + cs->len, name, name_len);
	cs->len += name_len;
	cs->path[cs->len] = '\0';

	if (census_entry(cs, d_type) && d_type == DT_DIR && cs->depth < MAX_DEPTH &&
	    (cs->max_depth == 0 || (uint32_t)cs->depth + 1 < cs->max_depth) &&
	    !census_skipped(cs)) {
		if (cs->par) {
			par_push(cs->path, cs->len, cs->depth + 1);
		} else {
			cs->depth++;
			census_dir(cs);
			cs->depth--;
		}
	}
	cs->len = saved;
	cs->path[cs->len] = '\0';
}

/* Every entry of the directory the path currently names. */
void
census_dir(struct census *cs)
{
	vnode_t dvp = NULLVP;
	char *dbuf;
	off_t offset = 0;
	int error;

	{
		uint64_t t0 = mach_absolute_time();

		error = vnode_lookup(cs->len != 0 ? cs->path : "/", cs->lookup_flags, &dvp,
		    cs->ctx);
		cs->t_lookup += mach_absolute_time() - t0;
	}
	if (error == 0 && cs->mp == NULL) {
		cs->mp = vnode_mount(dvp);
	}
	if (error) {
		printf("apfs-check: %s: lookup failed: %d\n",
		    cs->len != 0 ? cs->path + cs->rel : "/", error);
		cs->errors++;
		return;
	}
	dbuf = kalloc_data(DIR_CHUNK, Z_WAITOK);
	if (dbuf == NULL) {
		vnode_put(dvp);
		cs->errors++;
		return;
	}
	for (;;) {
		uio_t uio;
		char uio_buf[UIO_SIZEOF(1)];
		int eof = 0, items = 0;
		size_t got;
		char *p;

		uio = uio_createwithbuffer(1, offset, UIO_SYSSPACE, UIO_READ, uio_buf,
		    sizeof(uio_buf));
		if (uio == NULL) {
			cs->errors++;
			break;
		}
		uio_addiov(uio, CAST_USER_ADDR_T(dbuf), DIR_CHUNK);
		PHASE("readdir");
		{
			uint64_t t0 = mach_absolute_time();

			error = VNOP_READDIR(dvp, uio, 0, &eof, &items, cs->ctx);
			cs->t_readdir += mach_absolute_time() - t0;
		}
		if (error) {
			printf("apfs-check: %s: readdir failed: %d\n", cs->path + cs->rel, error);
			cs->errors++;
			break;
		}
		got = DIR_CHUNK - (size_t)uio_resid(uio);
		if (got == 0) {
			break;
		}
		offset = uio_offset(uio);

		for (p = dbuf; p < dbuf + got;) {
			struct dirent *de = (struct dirent *)p;

			if (de->d_reclen == 0 || p + de->d_reclen > dbuf + got) {
				printf("apfs-check: %s: a directory entry of %u bytes\n",
				    cs->path + cs->rel, de->d_reclen);
				cs->errors++;
				break;
			}
			p += de->d_reclen;
			if (de->d_namlen == 0 ||
			    (de->d_namlen == 1 && de->d_name[0] == '.') ||
			    (de->d_namlen == 2 && de->d_name[0] == '.' && de->d_name[1] == '.')) {
				continue;
			}
			census_child(cs, de->d_name, de->d_namlen, de->d_type);
			if (cs->stop) {
				break;
			}
		}
		if (eof || cs->stop) {
			break;
		}
	}
	kfree_data(dbuf, DIR_CHUNK);
	vnode_put(dvp);
}

/*
 * apfs_walkers=N (mode 6): N threads walk the volume at once. They take directories off one
 * shared stack -- a directory's own entries are done by the walker that took it, its
 * subdirectories pushed for any of them -- so that as many files are read at once as there are
 * walkers, and the storage has that many requests to work on. The per-line hashes add up the
 * same whatever the order: the sum and the counts are those of the walk on one thread.
 */
struct par_dir {
	char  *path;
	size_t len;
	int    depth;
};

static LCK_GRP_DECLARE(par_lck_grp, "apfs-check-walkers");
static LCK_MTX_DECLARE(par_lock, &par_lck_grp);
static struct par_dir *par_stack;
static uint32_t par_count, par_cap, par_busy, par_finished, par_most;
static bool par_failed;

static void
par_push(const char *path, size_t len, int depth)
{
	char *copy = kalloc_data(len + 1, Z_WAITOK);

	lck_mtx_lock(&par_lock);
	if (par_count == par_cap) {
		uint32_t cap = par_cap ? par_cap * 2 : 1024;
		struct par_dir *grown = kalloc_data(cap * sizeof(*grown), Z_WAITOK);

		if (grown == NULL) {
			par_failed = true;
			lck_mtx_unlock(&par_lock);
			kfree_data(copy, len + 1);
			return;
		}
		if (par_stack != NULL) {
			memcpy(grown, par_stack, par_count * sizeof(*grown));
			kfree_data(par_stack, par_cap * sizeof(*grown));
		}
		par_stack = grown;
		par_cap = cap;
	}
	if (copy == NULL) {
		par_failed = true;
	} else {
		memcpy(copy, path, len + 1);
		par_stack[par_count++] = (struct par_dir){ copy, len, depth };
		if (par_count > par_most) {
			par_most = par_count;
		}
		wakeup(&par_stack);
	}
	lck_mtx_unlock(&par_lock);
}

static void
par_walker(void *arg, __unused wait_result_t wr)
{
	struct census *cs = arg;
	uint64_t cpu0 = thread_get_runtime_self(), wall0 = mach_absolute_time();

	for (;;) {
		struct par_dir d;
		uint64_t t0 = mach_absolute_time();

		lck_mtx_lock(&par_lock);
		while (par_count == 0 && par_busy > 0) {
			msleep(&par_stack, &par_lock, PRIBIO, "apfs-walkers", NULL);
		}
		cs->t_idle += mach_absolute_time() - t0;
		if (par_count == 0) {
			/* nothing left, and no walker that could leave more */
			cs->t_cpu = thread_get_runtime_self() - cpu0;
			cs->t_wall = mach_absolute_time() - wall0;
			par_finished++;
			wakeup(&par_stack);
			lck_mtx_unlock(&par_lock);
			return;
		}
		d = par_stack[--par_count];
		par_busy++;
		lck_mtx_unlock(&par_lock);

		memcpy(cs->path, d.path, d.len + 1);
		cs->len = d.len;
		cs->depth = d.depth;
		kfree_data(d.path, d.len + 1);
		if (!cs->stop) {
			census_dir(cs);
		}

		lck_mtx_lock(&par_lock);
		par_busy--;
		wakeup(&par_stack);
		lck_mtx_unlock(&par_lock);
	}
}

/* where a walker's (or, summed, all walkers') time went */
void
census_times(const struct census *cs, int who)
{
	uint64_t v[8] = { cs->t_wall, cs->t_cpu, cs->t_lookup, cs->t_attr, cs->t_read, cs->hash_ticks, cs->t_readdir,
		              cs->t_idle };

	for (int i = 0; i < 8; i++) {
		absolutetime_to_nanoseconds(v[i], &v[i]);
		v[i] /= 1000000;
	}
	char name[24];

	if (who < 0) {
		strlcpy(name, "all walkers", sizeof(name));
	} else {
		snprintf(name, sizeof(name), "walker %d", who);
	}
	printf("apfs-check: %s: %llu entries, %llu bytes: %llu ms, on a processor %llu; lookups %llu, getattr %llu, "
	    "reading %llu (of it hashing %llu), readdir %llu, waiting for a directory %llu\n",
	    name, cs->entries, cs->bytes, v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7]);
}

/* The whole walk on n threads, its totals into cs */
void
par_walk(struct census *cs, uint32_t n)
{
	struct census *w[PAR_MAX];
	uint32_t started = 0;

	par_count = par_busy = par_finished = par_most = 0;
	par_failed = false;
	par_push(cs->path, cs->len, cs->depth);
	for (uint32_t i = 0; i < n; i++) {
		w[i] = kalloc_type(struct census, Z_WAITOK | Z_ZERO);
		if (w[i] == NULL) {
			break;
		}
		*w[i] = *cs;
		w[i]->par = true;
		w[i]->sum = w[i]->entries = w[i]->dirs = w[i]->files = w[i]->links = w[i]->others = w[i]->bytes = 0;
		w[i]->errors = 0;
		w[i]->hash_ticks = 0;
		w[i]->path = kalloc_data(PATH_CAP, Z_WAITOK | Z_ZERO);
		w[i]->buf = kalloc_data(READ_CHUNK, Z_WAITOK);
		if (w[i]->path == NULL || w[i]->buf == NULL) {
			break;
		}
		par_cs[i] = w[i];
		started = i + 1;
	}
	par_n = started;
	printf("apfs-check: %u walkers at once\n", started);
	for (uint32_t i = 0; i < started; i++) {
		thread_t th;

		if (kernel_thread_start(par_walker, w[i], &th) == KERN_SUCCESS) {
			thread_deallocate(th);
		} else {
			lck_mtx_lock(&par_lock);
			par_finished++;
			lck_mtx_unlock(&par_lock);
		}
	}
	lck_mtx_lock(&par_lock);
	while (par_finished < started) {
		msleep(&par_stack, &par_lock, PRIBIO, "apfs-walk", NULL);
	}
	lck_mtx_unlock(&par_lock);
	par_n = 0;
	for (uint32_t i = 0; i < started; i++) {
		par_cs[i] = NULL;
		cs->sum += w[i]->sum;
		cs->entries += w[i]->entries;
		cs->dirs += w[i]->dirs;
		cs->files += w[i]->files;
		cs->links += w[i]->links;
		cs->others += w[i]->others;
		cs->bytes += w[i]->bytes;
		cs->errors += w[i]->errors;
		cs->hash_ticks += w[i]->hash_ticks;
		census_times(w[i], i);
		cs->t_lookup += w[i]->t_lookup;
		cs->t_attr += w[i]->t_attr;
		cs->t_read += w[i]->t_read;
		cs->t_readdir += w[i]->t_readdir;
		cs->t_idle += w[i]->t_idle;
		cs->t_cpu += w[i]->t_cpu;
		cs->t_wall += w[i]->t_wall;
		kfree_data(w[i]->path, PATH_CAP);
		kfree_data(w[i]->buf, READ_CHUNK);
		kfree_type(struct census, w[i]);
	}
	if (par_failed) {
		printf("apfs-check: the walkers ran out of memory for their stack: the walk is NOT whole\n");
		cs->errors++;
	}
	census_times(cs, -1);
	printf("apfs-check: at most %u directories waited on the walkers' stack\n", par_most);
	if (par_stack != NULL) {
		kfree_data(par_stack, par_cap * sizeof(*par_stack));
		par_stack = NULL;
		par_cap = 0;
	}
}
