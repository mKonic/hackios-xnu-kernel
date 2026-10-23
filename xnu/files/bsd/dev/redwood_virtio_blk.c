/*
 * Virtio block devices: a disk image of the host becomes a block device of the guest,
 * /dev/rvblkN (block) and /dev/rrvblkN (character), N from 0 in the order of
 * sys/redwood_virtio.h's vio_find() (PCI first, then virtio-mmio).
 *
 * Any transport: virtio-pci under crosvm (the Android Virtualization Framework) or QEMU
 * (-device virtio-blk-pci), virtio-mmio under QEMU virt (-device virtio-blk-device with
 * -global virtio-mmio.force-legacy=false and boot-arg virtio_blk=1, which also turns that bus
 * on). boot-arg virtio_blk=0 leaves every disk alone.
 *
 * This exists for the APFS driver: the iOS system volume is several GB, too much to hold in
 * guest memory as a RAM disk. With rd=rvblk0 it is the root (IOFindBSDRoot), the host serving
 * it straight off its disk:
 *
 *   -drive file=rootfs.apfs,format=raw,if=none,id=d,readonly=on -device virtio-blk-pci,drive=d
 *
 * Read only unless boot-arg virtio_blk_rw=1 (and the device is not read-only itself): nothing
 * should write to an image by accident.
 *
 * Data moves straight between the device and the buffer's pages, a descriptor per physically
 * contiguous run; one request per disk is in flight at a time. Completion arrives by
 * interrupt when the transport has one (vio_intr_enable), else the used ring is polled.
 */

#include <kern/clock.h>
#include <kern/locks.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <mach/vm_param.h>
#include <miscfs/devfs/devfs.h>
#include <pexpert/pexpert.h>
#include <sys/buf.h>
#include <sys/conf.h>
#include <sys/disk.h>
#include <sys/errno.h>
#include <sys/param.h>
#include <sys/systm.h>
#include <sys/redwood_pci.h>
#include <sys/redwood_virtio.h>
#include <sys/redwood_virtio_blk.h>

extern uint64_t kvtophys(vm_offset_t va);

#define VIRTIO_BLK_F_SIZE_MAX   1
#define VIRTIO_BLK_F_SEG_MAX    2
#define VIRTIO_BLK_F_RO         5
#define VIRTIO_BLK_F_BLK_SIZE   6
#define VIRTIO_BLK_F_FLUSH      9

/* the device's configuration */
#define CFG_CAPACITY            0x00    /* u64, in 512-byte sectors */
#define CFG_SIZE_MAX            0x08    /* u32 */
#define CFG_SEG_MAX             0x0c    /* u32 */
#define CFG_BLK_SIZE            0x14    /* u32 */

#define VIRTIO_BLK_T_IN         0
#define VIRTIO_BLK_T_OUT        1
#define VIRTIO_BLK_T_FLUSH      4
#define VIRTIO_BLK_S_OK         0
#define VIRTIO_BLK_S_UNSUPP     2

#define VIRTIO_BLK_SECTOR       512     /* virtio-blk counts in these, always */
#define BLK_MAX_XFER            (1024 * 1024)
#define BLK_MAX_SEGS            (BLK_MAX_XFER / PAGE_SIZE + 1)
#define BLK_MAX_DISKS           4
#define BLK_TIMEOUT_NS          (30ull * NSEC_PER_SEC)

struct blk_req_hdr {
	uint32_t type;
	uint32_t reserved;
	uint64_t sector;
};

struct blk_disk {
	vio_dev_t               *dev;
	vio_queue_t             *queue;
	lck_mtx_t               lock;
	struct blk_req_hdr      *hdr;           /* one page: the header, then the status byte */
	volatile uint8_t        *status;
	uint64_t                hdr_pa;
	uint64_t                capacity;       /* in 512-byte sectors */
	uint32_t                blk_size;       /* the device's block (physical) size */
	uint32_t                max_segs;       /* data descriptors in one request */
	uint32_t                max_seg_size;
	bool                    writable, flush;
	uint64_t                requests, errors;
};

static LCK_GRP_DECLARE(blk_lck_grp, "redwood-virtio-blk");
static struct blk_disk blk_disks[BLK_MAX_DISKS];
static unsigned blk_count;
static bool blk_initialized;
static int blk_bmajor = -1, blk_cmajor = -1;

/* One of this driver's devices: every request is a trip through the hypervisor */
bool
redwood_virtio_blk_dev(dev_t dev)
{
	return (blk_bmajor >= 0 && major(dev) == blk_bmajor) ||
	       (blk_cmajor >= 0 && major(dev) == blk_cmajor);
}

static struct blk_disk *
disk_of(dev_t dev)
{
	unsigned unit = (unsigned)minor(dev);
	return unit < blk_count ? &blk_disks[unit] : NULL;
}

/*
 * One request, under the disk's lock: header, data (absent for a flush) and status, then
 * wait for the device to hand it back.
 */
static int
blk_request(struct blk_disk *d, uint32_t type, uint64_t sector, vm_offset_t data, size_t len)
{
	struct vio_sg sg[BLK_MAX_SEGS + 2];
	unsigned n = 0;
	uint64_t deadline;
	uint32_t seq;
	void *done;

	d->hdr->type = type;
	d->hdr->reserved = 0;
	d->hdr->sector = sector;
	*d->status = 0xff;
	sg[n++] = (struct vio_sg){ d->hdr_pa, sizeof(struct blk_req_hdr), false };
	for (size_t at = 0; at < len;) {
		vm_offset_t va = data + at;
		size_t chunk = MIN(PAGE_SIZE - (va & PAGE_MASK), len - at);
		uint64_t pa = kvtophys(va);

		if (pa == 0) {
			return EFAULT;
		}
		/* the pages of a buffer are often next to each other: one descriptor for the run */
		if (n > 1 && sg[n - 1].pa + sg[n - 1].len == pa && sg[n - 1].len + chunk <= d->max_seg_size) {
			sg[n - 1].len += (uint32_t)chunk;
		} else {
			if (n - 1 == d->max_segs) {
				return E2BIG;
			}
			sg[n++] = (struct vio_sg){ pa, (uint32_t)chunk, type == VIRTIO_BLK_T_IN };
		}
		at += chunk;
	}
	sg[n++] = (struct vio_sg){ d->hdr_pa + offsetof(struct blk_req_hdr, sector) + 64, 1, true };

	seq = vio_intr_seq(d->dev);
	if (vio_queue_add(d->queue, sg, n, d) != 0) {
		return EIO;
	}
	vio_queue_kick(d->queue);
	d->requests++;
	nanoseconds_to_deadline(BLK_TIMEOUT_NS, &deadline);
	while ((done = vio_queue_get(d->queue, NULL)) == NULL) {
		if (mach_absolute_time() > deadline) {
			printf("virtio-blk: %s: a request of %lu bytes at sector %llu went unanswered; the disk is off\n",
			    vio_name(d->dev), (unsigned long)len, sector);
			vio_fail(d->dev);
			d->capacity = 0;
			return EIO;
		}
		vio_intr_wait(d->dev, seq, 10 * NSEC_PER_MSEC);
		seq = vio_intr_seq(d->dev);
	}
	if (*d->status != VIRTIO_BLK_S_OK) {
		d->errors++;
		if (d->errors < 10) {
			printf("virtio-blk: %s: request %u of %lu bytes at sector %llu: status %u\n",
			    vio_name(d->dev), type, (unsigned long)len, sector, *d->status);
		}
		return *d->status == VIRTIO_BLK_S_UNSUPP ? ENOTSUP : EIO;
	}
	return 0;
}

/* ---- the block device ---- */

static int
blk_dev_open(dev_t dev, int flags, __unused int devtype, __unused proc_t p)
{
	struct blk_disk *d = disk_of(dev);

	if (d == NULL || d->capacity == 0) {
		return ENXIO;
	}
	if ((flags & FWRITE) && !d->writable) {
		return EROFS;
	}
	return 0;
}

static int
blk_dev_close(__unused dev_t dev, __unused int flags, __unused int devtype, __unused proc_t p)
{
	return 0;
}

static void
blk_dev_strategy(buf_t bp)
{
	struct blk_disk *d = disk_of(buf_device(bp));
	bool read = buf_flags(bp) & B_READ;
	uint64_t offset, size;
	size_t len, done = 0;
	caddr_t va = NULL;
	int error = 0;

	buf_setresid(bp, buf_count(bp));
	if (d == NULL || d->capacity == 0) {
		error = ENXIO;
		goto out;
	}
	if (!read && !d->writable) {
		error = EROFS;
		goto out;
	}
	offset = (uint64_t)buf_blkno(bp) * VIRTIO_BLK_SECTOR;
	size = d->capacity * VIRTIO_BLK_SECTOR;
	len = buf_count(bp);
	/* trim a request that runs off the end, and refuse one that starts past it */
	if (offset >= size) {
		error = offset == size ? 0 : EINVAL;
		goto out;
	}
	if (len > size - offset) {
		len = (size_t)(size - offset);
	}
	if (len % VIRTIO_BLK_SECTOR) {
		error = EINVAL;
		goto out;
	}
	if (len == 0) {
		goto out;
	}
	if (buf_map(bp, &va) != 0) {
		error = EIO;
		goto out;
	}
	lck_mtx_lock(&d->lock);
	while (done < len && error == 0) {
		size_t chunk = MIN(len - done, (size_t)BLK_MAX_XFER);

		error = blk_request(d, read ? VIRTIO_BLK_T_IN : VIRTIO_BLK_T_OUT,
		    (offset + done) / VIRTIO_BLK_SECTOR, (vm_offset_t)va + done, chunk);
		if (error == E2BIG) {
			/* too scattered for the device's segment limit: in smaller pieces */
			size_t piece = (size_t)MAX(d->max_segs - 1, 1u) * PAGE_SIZE;
			error = 0;
			for (size_t at = 0; at < chunk && error == 0; at += piece) {
				error = blk_request(d, read ? VIRTIO_BLK_T_IN : VIRTIO_BLK_T_OUT,
				    (offset + done + at) / VIRTIO_BLK_SECTOR, (vm_offset_t)va + done + at,
				    MIN(piece, chunk - at));
			}
		}
		if (error == 0) {
			done += chunk;
		}
	}
	lck_mtx_unlock(&d->lock);
	buf_unmap(bp);
	buf_setresid(bp, (uint32_t)(buf_count(bp) - done));
out:
	if (error) {
		buf_seterror(bp, error);
	}
	buf_biodone(bp);
}

static int
blk_dev_ioctl(dev_t dev, u_long cmd, caddr_t data, __unused int fflag, __unused proc_t p)
{
	struct blk_disk *d = disk_of(dev);
	int error;

	if (d == NULL || d->capacity == 0) {
		return ENXIO;
	}
	switch (cmd) {
	case DKIOCGETBLOCKSIZE:
		*(uint32_t *)data = VIRTIO_BLK_SECTOR;
		return 0;
	case DKIOCSETBLOCKSIZE:
		/*
		 * spec_open() sets the block size to 512 to count the device in 512-byte
		 * blocks and then sets it back, and treats a refusal of either as the device
		 * being broken (ENXIO). virtio-blk addresses 512-byte sectors and nothing else.
		 */
		return *(uint32_t *)data == VIRTIO_BLK_SECTOR ? 0 : EINVAL;
	case DKIOCGETPHYSICALBLOCKSIZE:
		*(uint32_t *)data = d->blk_size;
		return 0;
	case DKIOCGETBLOCKCOUNT:
		*(uint64_t *)data = d->capacity;
		return 0;
	case DKIOCISWRITABLE:
		*(uint32_t *)data = d->writable;
		return 0;
	case DKIOCGETMAXBLOCKCOUNTREAD:
	case DKIOCGETMAXBLOCKCOUNTWRITE:
		*(uint64_t *)data = BLK_MAX_XFER / VIRTIO_BLK_SECTOR;
		return 0;
	case DKIOCGETMAXSEGMENTCOUNTREAD:
	case DKIOCGETMAXSEGMENTCOUNTWRITE:
		*(uint64_t *)data = d->max_segs;
		return 0;
	case DKIOCSYNCHRONIZE:
	case DKIOCSYNCHRONIZECACHE:
		if (!d->writable || !d->flush) {
			return 0;
		}
		lck_mtx_lock(&d->lock);
		error = blk_request(d, VIRTIO_BLK_T_FLUSH, 0, 0, 0);
		lck_mtx_unlock(&d->lock);
		return error;
	default:
		return ENOTTY;
	}
}

static int
blk_dev_size(dev_t dev)
{
	struct blk_disk *d = disk_of(dev);
	return d && d->capacity ? VIRTIO_BLK_SECTOR : -1;
}

static int
blk_dev_read(dev_t dev, struct uio *uio, __unused int ioflag)
{
	return physio(blk_dev_strategy, NULL, dev, B_READ, minphys, uio, VIRTIO_BLK_SECTOR);
}

static int
blk_dev_write(dev_t dev, struct uio *uio, __unused int ioflag)
{
	struct blk_disk *d = disk_of(dev);

	if (d == NULL || !d->writable) {
		return EROFS;
	}
	return physio(blk_dev_strategy, NULL, dev, B_WRITE, minphys, uio, VIRTIO_BLK_SECTOR);
}

static const struct bdevsw blk_bdevsw = {
	.d_open     = blk_dev_open,
	.d_close    = blk_dev_close,
	.d_strategy = blk_dev_strategy,
	.d_ioctl    = blk_dev_ioctl,
	.d_dump     = eno_dump,
	.d_psize    = blk_dev_size,
	.d_type     = D_DISK,
};

static const struct cdevsw blk_cdevsw = {
	.d_open       = blk_dev_open,
	.d_close      = blk_dev_close,
	.d_read       = blk_dev_read,
	.d_write      = blk_dev_write,
	.d_ioctl      = blk_dev_ioctl,
	.d_stop       = eno_stop,
	.d_reset      = eno_reset,
	.d_ttys       = NULL,
	.d_select     = eno_select,
	.d_mmap       = eno_mmap,
	.d_strategy   = eno_strat,
	.d_reserved_1 = eno_getc,
	.d_reserved_2 = eno_putc,
	.d_type       = D_DISK,
};

/* ---- bringing it up ---- */

static bool
blk_attach(struct blk_disk *d, vio_dev_t *dev, bool rw_wanted)
{
	uint64_t features;
	int error;

	d->dev = dev;
	error = vio_init(dev, VIRTIO_BIT(VIRTIO_BLK_F_SIZE_MAX) | VIRTIO_BIT(VIRTIO_BLK_F_SEG_MAX) |
	    VIRTIO_BIT(VIRTIO_BLK_F_RO) | VIRTIO_BIT(VIRTIO_BLK_F_BLK_SIZE) | VIRTIO_BIT(VIRTIO_BLK_F_FLUSH), &features);
	if (error) {
		printf("virtio-blk: %s: features refused (%d)\n", vio_name(dev), error);
		return false;
	}
	d->capacity = vio_cfg_read64(dev, CFG_CAPACITY);
	d->blk_size = (features & VIRTIO_BIT(VIRTIO_BLK_F_BLK_SIZE)) ? vio_cfg_read32(dev, CFG_BLK_SIZE) : VIRTIO_BLK_SECTOR;
	if (d->blk_size < VIRTIO_BLK_SECTOR || (d->blk_size & (d->blk_size - 1))) {
		d->blk_size = VIRTIO_BLK_SECTOR;
	}
	d->max_seg_size = (features & VIRTIO_BIT(VIRTIO_BLK_F_SIZE_MAX)) ? vio_cfg_read32(dev, CFG_SIZE_MAX) : 0;
	if (d->max_seg_size < PAGE_SIZE) {
		d->max_seg_size = d->max_seg_size ? PAGE_SIZE : BLK_MAX_XFER;
	}
	d->max_segs = (features & VIRTIO_BIT(VIRTIO_BLK_F_SEG_MAX)) ? vio_cfg_read32(dev, CFG_SEG_MAX) : BLK_MAX_SEGS;
	d->max_segs = MAX(1u, MIN(d->max_segs, (uint32_t)BLK_MAX_SEGS));
	d->writable = rw_wanted && !(features & VIRTIO_BIT(VIRTIO_BLK_F_RO));
	d->flush = (features & VIRTIO_BIT(VIRTIO_BLK_F_FLUSH)) != 0;
	if (d->capacity == 0) {
		printf("virtio-blk: %s: the disk is empty\n", vio_name(dev));
		vio_fail(dev);
		return false;
	}
	if ((error = vio_queue_setup(dev, 0, 0, &d->queue)) != 0) {
		printf("virtio-blk: %s: no request queue (%d)\n", vio_name(dev), error);
		vio_fail(dev);
		return false;
	}
	d->max_segs = MIN(d->max_segs, (uint32_t)vio_queue_size(d->queue) - 2);
	if ((d->hdr = vio_dma_alloc(PAGE_SIZE, &d->hdr_pa)) == NULL) {
		vio_fail(dev);
		return false;
	}
	/* the status byte: 64 bytes after the header's sector field, in the same page */
	d->status = (volatile uint8_t *)d->hdr + offsetof(struct blk_req_hdr, sector) + 64;
	lck_mtx_init(&d->lock, &blk_lck_grp, LCK_ATTR_NULL);
	(void)vio_intr_enable(dev);
	vio_driver_ok(dev);
	return true;
}

void
redwood_virtio_blk_init(void)
{
	uint32_t enabled = 1, rw = 0;
	vio_dev_t *dev;

	if (blk_initialized) {
		return;
	}
	blk_initialized = true;
	if (PE_parse_boot_argn("virtio_blk", &enabled, sizeof(enabled)) && !enabled) {
		return;
	}
	PE_parse_boot_argn("virtio_blk_rw", &rw, sizeof(rw));
	redwood_pci_dump();
	for (unsigned nth = 0; blk_count < BLK_MAX_DISKS && (dev = vio_find(VIRTIO_ID_BLOCK, nth)) != NULL; nth++) {
		struct blk_disk *d = &blk_disks[blk_count];
		char name[16];

		if (!blk_attach(d, dev, rw != 0)) {
			continue;
		}
		if (blk_bmajor < 0) {
			blk_bmajor = bdevsw_add(-1, &blk_bdevsw);
			blk_cmajor = blk_bmajor < 0 ? -1 : cdevsw_add_with_bdev(-1, &blk_cdevsw, blk_bmajor);
			if (blk_cmajor < 0) {
				printf("virtio-blk: no device switch entry\n");
				vio_fail(dev);
				return;
			}
		}
		snprintf(name, sizeof(name), "rvblk%u", blk_count);
		if (devfs_make_node(makedev(blk_bmajor, blk_count), DEVFS_BLOCK, UID_ROOT, GID_OPERATOR, 0640,
		    "%s", name) == NULL ||
		    devfs_make_node(makedev(blk_cmajor, blk_count), DEVFS_CHAR, UID_ROOT, GID_OPERATOR, 0640,
		    "r%s", name) == NULL) {
			printf("virtio-blk: devfs_make_node failed\n");
		}
		printf("virtio-blk: /dev/%s on %s: %llu sectors (%llu MB), blocks of %u, %u segments, %s%s\n",
		    name, vio_name(dev), d->capacity, d->capacity * VIRTIO_BLK_SECTOR / (1024 * 1024), d->blk_size,
		    d->max_segs, d->writable ? "read-write" : "read-only",
		    vio_intr_active(dev) ? ", interrupts" : ", polled");
		blk_count++;
	}
	if (blk_count == 0) {
		printf("virtio-blk: no block device\n");
	}
}

int
redwood_virtio_blk_rootdev(const char *name, dev_t *dev)
{
	unsigned unit = 0;

	if (strncmp(name, "rvblk", 5) != 0 || name[5] < '0' || name[5] > '9' || name[6] != 0) {
		return EINVAL;
	}
	unit = (unsigned)(name[5] - '0');
	redwood_virtio_blk_init();
	if (unit >= blk_count || blk_bmajor < 0) {
		return ENXIO;
	}
	*dev = makedev(blk_bmajor, unit);
	return 0;
}

/*
 * One more pair of names for a disk: /dev/NAME and /dev/rNAME (md0=rvblk0 makes the system
 * disk /dev/md0 for HackiOS's user space, IOFindBSDRoot).
 */
int
redwood_virtio_blk_alias(const char *disk, const char *name)
{
	dev_t dev;
	int error;

	if ((error = redwood_virtio_blk_rootdev(disk, &dev)) != 0) {
		return error;
	}
	if (devfs_make_node(dev, DEVFS_BLOCK, UID_ROOT, GID_OPERATOR, 0640, "%s", name) == NULL ||
	    devfs_make_node(makedev(blk_cmajor, minor(dev)), DEVFS_CHAR, UID_ROOT, GID_OPERATOR, 0640, "r%s", name) == NULL) {
		return ENOMEM;
	}
	printf("virtio-blk: /dev/%s is /dev/%s\n", name, disk);
	return 0;
}
