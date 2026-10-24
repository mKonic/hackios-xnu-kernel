/*
 * Mount the APFS volume on /dev/rvblk0 and walk it, to compare with what the host reader
 * sees in the same image.  Only with boot-arg apfs_check.
 * See bsd/dev/redwood_apfs_check.c.
 */
#ifndef _SYS_REDWOOD_APFS_CHECK_H_
#define _SYS_REDWOOD_APFS_CHECK_H_

void redwood_apfs_check_init(void);


/*
 * Inside the checker.  The three files in bsd/dev split this up: the walking machinery
 * lives in redwood_apfs_check_walk.c, the writing in redwood_apfs_check_write.c, and
 * redwood_apfs_check.c itself mounts the volume, runs the modes and prints.  What the
 * pieces share is here.
 */
#include <mach/mach_types.h>        /* mount_t */
#include <pexpert/pexpert.h>        /* PE_parse_boot_argn, for read_chunk below */
#include <stdbool.h>
#include <stdint.h>
#include <sys/vnode.h>              /* vfs_context_t */

#define CHECK_MNT    "/private/var/apfs-check"
#define CHECK_PARENT "/private/var"
#define CHECK_LEAF   "apfs-check"
#define CHECK_DEV    "/dev/rvblk0"

#define FNV_INIT  0xcbf29ce484222325ULL
#define FNV_PRIME 0x100000001b3ULL

/*
 * How much each read asks for: 64 KB, one compressed piece, unless boot-arg apfs_chunk says
 * otherwise (up to 4 MB) -- a bigger read is one request whose pieces the driver can
 * decompress on several processors at once.
 */
static inline uint32_t
read_chunk(void)
{
	static uint32_t chunk;

	if (chunk == 0) {
		uint32_t want = 64 * 1024;

		PE_parse_boot_argn("apfs_chunk", &want, sizeof(want));
		if (want < 4096 || want > 4 * 1024 * 1024) {
			want = 64 * 1024;
		}
		chunk = want;
	}
	return chunk;
}
#define READ_CHUNK read_chunk()

#define PATH_CAP   2048
/* the walker ceiling of a parallel walk: the dispatch in redwood_apfs_check.c clamps
 * apfs_walkers with it, the queue in redwood_apfs_check_walk.c is sized by it */
#define PAR_MAX    16

struct census {
	vfs_context_t ctx;
	char     *path;                 /* the whole path, starting with CHECK_MNT */
	size_t    rel;                  /* where the part inside the volume starts */
	size_t    len;
	uint8_t  *buf;                  /* for reading file contents */
	uint64_t  sum;                  /* the per-line hashes, added up */
	uint64_t  entries, dirs, files, links, others, bytes;
	uint32_t  errors;
	bool      print_lines;
	int       depth;
	/*
	 * The volume being walked.  Walking the root means walking over the mount points of
	 * other filesystems -- devfs on /dev above all -- and their contents are not this
	 * volume's, so anything that turns out to live on another mount is passed over.
	 */
	mount_t   mp;
	/*
	 * NOCROSSMOUNT when the volume being walked is the root: a lookup then gives the
	 * directory this volume has under a mount point rather than the root of whatever is
	 * mounted on it, so /dev is counted as the (empty) directory it is here instead of
	 * being either skipped or walked as devfs.  Not set when the volume is reached
	 * through a path that itself crosses into it.
	 */
	int       lookup_flags;
	uint32_t  max_depth;            /* 0: no limit */
	char     *skip;                 /* comma separated paths not to descend into */
	uint64_t  max_entries;          /* 0: no limit */
	bool      stop;
	uint64_t  t0;                   /* when the walk started, for the progress line */
	/*
	 * FNV-1a goes a byte at a time, each step waiting on the one before: about three
	 * cycles a byte, more than a second over the restore ramdisk -- the checker's own
	 * time, not the filesystem's.  Boot-arg apfs_hash=0 reads every byte all the same and
	 * does not hash it (the sums are then not the ones to compare); hash_ticks is how
	 * long the hashing took otherwise.
	 */
	bool      no_hash;
	uint64_t  hash_ticks;
	/*
	 * Boot-arg apfs_contents=0: no file is read at all, only looked up and stat'ed --
	 * how long the walk itself takes, the directories and the inodes.
	 */
	bool      no_contents;
	/* apfs_walkers=N: one of N walkers; a subdirectory is left on the shared stack (par_push) */
	bool      par;
	/* where a walker's time goes (mach ticks): lookups, getattr, reading files and links
	 * (hashing included), readdir, waiting for a directory to walk (par); on a processor */
	uint64_t  t_lookup, t_attr, t_read, t_readdir, t_idle, t_cpu, t_wall;
};

/* the walk, bsd/dev/redwood_apfs_check_walk.c */
void    census_options(struct census *cs);
void    census_dir(struct census *cs);
void    watch_start(struct census *cs);
void    watch_stop(void);
void    census_times(const struct census *cs, int who);
void    par_walk(struct census *cs, uint32_t n);

/* the writing, bsd/dev/redwood_apfs_check_write.c */
void    apfs_write_check(const char *devname);
void    apfs_store_check(const char *devname);

#endif /* _SYS_REDWOOD_APFS_CHECK_H_ */
