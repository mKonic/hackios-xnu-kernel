/*
 * The kernel's side of virtio-gpu 3D (bsd/dev/redwood_vgpu3d.c), on the control queue of the
 * virtio-gpu driver (bsd/dev/redwood_vgpu.c, redwood_vgpu_ctrl), which owns the device.
 *
 * /dev/vgpu3d (a cloning node: every open is its own minor): one virtio-gpu context and one
 * gfxstream pipe ("pipe:opengles", a render thread on the host) per open.
 *
 *   write(fd, buf, n)    the bytes go to the render thread (TRANSFER_TO_HOST_3D, 1 MiB at a time)
 *   read(fd, buf, n)     exactly n bytes of its replies (TRANSFER_FROM_HOST_3D)
 *   VGPU3D_IOC_CREATE    a 2D resource of this context (a gfxstream colour buffer whose handle is
 *                        the resource ID: rcBindTexture etc.), backed by kernel memory, width * 4
 *                        bytes a row
 *   VGPU3D_IOC_UPLOAD    a rectangle of user memory (any stride) into it, then to the host
 *   VGPU3D_IOC_DOWNLOAD  a rectangle from the host, then out to user memory
 *   VGPU3D_IOC_UNREF     a resource away (all of them go with the last close)
 *
 * The user-space side is hackios-xnu-userland's gfx library (src/gfx, gfx_dev.c).
 *
 * Boot-arg gpu3d=1 also runs vgpu3d_selftest() once at boot (gpu3d=2: in a thread): the host
 * GPU draws a picture, the guest reads it back and checks it ("GPU3D ..." lines).
 */
#include <IOKit/IOLib.h>
#include <kern/clock.h>
#include <kern/locks.h>
#include <kern/thread.h>
#include <libkern/libkern.h>
#include <mach/mach_time.h>
#include <mach/mach_types.h>
#include <mach/vm_param.h>
#include <miscfs/devfs/devfs.h>
#include <pexpert/pexpert.h>
#include <sys/conf.h>
#include <sys/errno.h>
#include <sys/fcntl.h>
#include <sys/ioccom.h>
#include <sys/param.h>
#include <sys/systm.h>
#include <sys/uio.h>
#include <vm/vm_kern.h>
#include <sys/redwood_vgpu.h>
#include <sys/redwood_vgpu3d.h>

extern uint64_t kvtophys(vm_offset_t va);

struct vgpu3d_ioc_create {
	uint32_t width, height, format, bind;   /* format: VIRGL_FORMAT_*, 0 = B8G8R8A8 */
	uint32_t res;                           /* out */
};
struct vgpu3d_ioc_xfer {
	uint32_t res, x, y, w, h;
	uint32_t stride;                        /* of the user memory, bytes */
	uint64_t addr;                          /* user address of pixel (x, y) */
};
#define VGPU3D_IOC_CREATE       _IOWR('G', 1, struct vgpu3d_ioc_create)
#define VGPU3D_IOC_UNREF        _IOW('G', 2, uint32_t)
#define VGPU3D_IOC_UPLOAD       _IOW('G', 3, struct vgpu3d_ioc_xfer)
#define VGPU3D_IOC_DOWNLOAD     _IOW('G', 4, struct vgpu3d_ioc_xfer)

#define MAX_OPENS               32
#define MAX_RES                 64
#define PIPE_SIZE               (1u << 20)
#define MAX_ENTRIES             3000            /* ATTACH_BACKING entries: 48 KiB of command */

struct res {
	uint32_t id, width, height;
	vm_offset_t va;
	vm_size_t size;
};

struct vopen {
	bool used, ready;
	lck_mtx_t lock;
	struct vgpu3d g;
	struct res res[MAX_RES];
	uint32_t next_id;
};

static LCK_GRP_DECLARE(vgpu3d_lck_grp, "redwood-vgpu3d");
static LCK_MTX_DECLARE(vgpu3d_opens_lock, &vgpu3d_lck_grp);
static struct vopen *opens[MAX_OPENS];
static int vgpu3d_major = -1;
static uint64_t vgpu3d_features;
static uint32_t vgpu3d_gles = 0xffffffff;       /* the context type: unknown yet */
static uint8_t *attach_scratch;                 /* ATTACH_BACKING commands (under vgpu3d_opens_lock) */

/* ---- the ops ---------------------------------------------------------------------------- */

static int
xnu_ctrl(__unused void *arg, const void *cmd, uint32_t cmd_len, void *resp, uint32_t resp_len)
{
	return redwood_vgpu_ctrl(cmd, cmd_len, resp, resp_len);
}

static void *
xnu_dma_alloc(__unused void *arg, size_t size, uint64_t *pa)
{
	vm_offset_t va = 0;
	if (kmem_alloc_contig(kernel_map, &va, round_page(size), PAGE_MASK, 0, 0,
	    KMA_KOBJECT | KMA_ZERO | KMA_DATA, VM_KERN_MEMORY_IOKIT) != KERN_SUCCESS) {
		return NULL;
	}
	*pa = kvtophys(va);
	return (void *)va;
}

static void
xnu_log(__unused void *arg, const char *line)
{
	printf("%s\n", line);
}

static uint64_t
xnu_now_ns(__unused void *arg)
{
	uint64_t ns;
	absolutetime_to_nanoseconds(mach_absolute_time(), &ns);
	return ns;
}

static const struct vgpu3d_ops xnu_ops = { NULL, xnu_ctrl, xnu_dma_alloc, xnu_log, xnu_now_ns };

/*
 * Once: bring the device up through the 2D driver, see that it does 3D, and pick the context
 * type (gfxstream-gles when offered, else the host's default).
 */
static int
vgpu3d_probe(void)
{
	static struct vgpu3d q;
	struct virtio_gpu_ctrl_hdr get = { .type = VIRTIO_GPU_CMD_GET_DISPLAY_INFO };
	static struct virtio_gpu_resp_display_info info;

	if (vgpu3d_gles != 0xffffffff) {
		return vgpu3d_gles == 0xfffffffe ? ENODEV : 0;
	}
	vgpu3d_gles = 0xfffffffe;
	if (redwood_vgpu_ctrl(&get, sizeof(get), &info, sizeof(info)) != 0) {
		printf("vgpu3d: no virtio-gpu device (does the VM have a GpuConfig?)\n");
		return ENODEV;
	}
	vgpu3d_features = redwood_vgpu_features();
	q.ops = xnu_ops;
	if (vgpu3d_query(&q, vgpu3d_features, VGPU3D_PROBE_CAPSETS) != 0) {
		return ENODEV;
	}
	vgpu3d_gles = vgpu3d_has_capset(&q, VIRTIO_GPU_CAPSET_GFXSTREAM_GLES) ? VIRTIO_GPU_CAPSET_GFXSTREAM_GLES : 0;
	attach_scratch = IOMallocData(sizeof(struct virtio_gpu_resource_attach_backing) +
	    MAX_ENTRIES * sizeof(struct virtio_gpu_mem_entry));
	return attach_scratch ? 0 : ENOMEM;
}

/* ---- resources -------------------------------------------------------------------------- */

static struct res *
res_find(struct vopen *o, uint32_t id)
{
	for (int i = 0; i < MAX_RES; i++) {
		if (o->res[i].id == id && id) {
			return &o->res[i];
		}
	}
	return NULL;
}

static void
res_free(struct vopen *o, struct res *r)
{
	vgpu3d_resource_unref(&o->g, r->id);
	o->g.err = 0;
	kmem_free(kernel_map, r->va, r->size);
	bzero(r, sizeof(*r));
}

static int
res_create(struct vopen *o, struct vgpu3d_ioc_create *c)
{
	struct res *r = res_find(o, 0);
	struct virtio_gpu_mem_entry *e;
	uint32_t n = 0;
	int err;

	for (int i = 0; r == NULL && i < MAX_RES; i++) {
		if (o->res[i].id == 0) {
			r = &o->res[i];
		}
	}
	if (r == NULL || c->width == 0 || c->height == 0 || c->width > 8192 || c->height > 8192) {
		return r == NULL ? ENOSPC : EINVAL;
	}
	r->size = round_page((vm_size_t)c->width * c->height * 4);
	if (kmem_alloc(kernel_map, &r->va, r->size, KMA_KOBJECT | KMA_ZERO | KMA_DATA, VM_KERN_MEMORY_IOKIT) !=
	    KERN_SUCCESS) {
		r->va = 0;
		return ENOMEM;
	}
	lck_mtx_lock(&vgpu3d_opens_lock);
	e = (struct virtio_gpu_mem_entry *)(attach_scratch + sizeof(struct virtio_gpu_resource_attach_backing));
	for (vm_size_t off = 0; off < r->size; off += PAGE_SIZE) {
		uint64_t pa = kvtophys(r->va + off);
		if (n && e[n - 1].addr + e[n - 1].length == pa) {
			e[n - 1].length += PAGE_SIZE;
		} else if (n < MAX_ENTRIES) {
			e[n].addr = pa;
			e[n].length = PAGE_SIZE;
			e[n].padding = 0;
			n++;
		} else {
			lck_mtx_unlock(&vgpu3d_opens_lock);
			kmem_free(kernel_map, r->va, r->size);
			bzero(r, sizeof(*r));
			return E2BIG;
		}
	}
	r->id = o->next_id++;
	r->width = c->width;
	r->height = c->height;
	/* the entries go into the scratch at the place the command wants them */
	struct virtio_gpu_mem_entry *ents = IOMallocData(n * sizeof(*ents));
	if (ents == NULL) {
		lck_mtx_unlock(&vgpu3d_opens_lock);
		kmem_free(kernel_map, r->va, r->size);
		bzero(r, sizeof(*r));
		return ENOMEM;
	}
	memcpy(ents, e, n * sizeof(*ents));
	err = vgpu3d_resource_create(&o->g, r->id, c->width, c->height,
	    c->format ? c->format : VIRGL_FORMAT_B8G8R8A8_UNORM,
	    c->bind ? c->bind : VIRGL_BIND_RENDER_TARGET | VIRGL_BIND_SAMPLER_VIEW, ents, n, attach_scratch,
	    (uint32_t)(sizeof(struct virtio_gpu_resource_attach_backing) + MAX_ENTRIES * sizeof(*ents)));
	lck_mtx_unlock(&vgpu3d_opens_lock);
	IOFreeData(ents, n * sizeof(*ents));
	if (err) {
		o->g.err = 0;
		kmem_free(kernel_map, r->va, r->size);
		bzero(r, sizeof(*r));
		return EIO;
	}
	c->res = r->id;
	return 0;
}

/* A rectangle between user memory and the backing, and the matching transfer. */
static int
res_xfer(struct vopen *o, struct vgpu3d_ioc_xfer *x, bool upload)
{
	struct res *r = res_find(o, x->res);
	int err = 0;

	if (r == NULL) {
		return ENOENT;
	}
	if (x->w == 0 || x->h == 0 || x->x >= r->width || x->y >= r->height || x->w > r->width - x->x ||
	    x->h > r->height - x->y || x->stride < x->w * 4) {
		return EINVAL;
	}
	size_t row = (size_t)x->w * 4, pitch = (size_t)r->width * 4;
	uint64_t offset = (uint64_t)x->y * pitch + (uint64_t)x->x * 4;
	uint8_t *base = (uint8_t *)r->va + offset;

	if (upload) {
		for (uint32_t y = 0; y < x->h && !err; y++) {
			err = copyin((user_addr_t)(x->addr + (uint64_t)y * x->stride), base + y * pitch, row);
		}
		if (!err && vgpu3d_transfer_box(&o->g, r->id, true, x->x, x->y, x->w, x->h, offset)) {
			o->g.err = 0;
			err = EIO;
		}
	} else {
		if (vgpu3d_transfer_box(&o->g, r->id, false, x->x, x->y, x->w, x->h, offset)) {
			o->g.err = 0;
			return EIO;
		}
		for (uint32_t y = 0; y < x->h && !err; y++) {
			err = copyout(base + y * pitch, (user_addr_t)(x->addr + (uint64_t)y * x->stride), row);
		}
	}
	return err;
}

/* ---- the device --------------------------------------------------------------------------- */

static int
vgpu3d_clone(__unused dev_t dev, int action)
{
	if (action == DEVFS_CLONE_ALLOC) {
		for (int i = 0; i < MAX_OPENS; i++) {
			if (opens[i] == NULL) {
				return i;
			}
		}
		return -1;
	}
	return 0;
}

static int
vgpu3d_dev_open(dev_t dev, __unused int flags, __unused int devtype, __unused proc_t p)
{
	int m = minor(dev);
	struct vopen *o;
	int err;

	if (m < 0 || m >= MAX_OPENS) {
		return ENXIO;
	}
	lck_mtx_lock(&vgpu3d_opens_lock);
	err = vgpu3d_probe();
	if (err == 0 && opens[m] != NULL) {
		err = EBUSY;
	}
	if (err == 0) {
		o = IOMallocZeroData(sizeof(*o));
		if (o == NULL) {
			err = ENOMEM;
		} else {
			opens[m] = o;
		}
	}
	lck_mtx_unlock(&vgpu3d_opens_lock);
	if (err) {
		return err;
	}
	lck_mtx_init(&o->lock, &vgpu3d_lck_grp, LCK_ATTR_NULL);
	o->g.ops = xnu_ops;
	o->g.features = vgpu3d_features;
	o->g.ctx_id = (uint32_t)m + 2;                            /* 1: the boot self-test's */
	o->g.next_resource = 0x20000 + ((uint32_t)m << 12);       /* the pipe */
	o->next_id = o->g.next_resource + 1;                      /* resources: 4095 a minor */
	if (vgpu3d_open(&o->g, vgpu3d_gles, PIPE_SIZE) != 0) {
		printf("vgpu3d: /dev/vgpu3d minor %d: no pipe (%d)\n", m, o->g.err);
		vgpu3d_close(&o->g);
		lck_mtx_lock(&vgpu3d_opens_lock);
		opens[m] = NULL;
		lck_mtx_unlock(&vgpu3d_opens_lock);
		lck_mtx_destroy(&o->lock, &vgpu3d_lck_grp);
		IOFreeData(o, sizeof(*o));
		return EIO;
	}
	o->ready = true;
	o->used = true;
	return 0;
}

static int
vgpu3d_dev_close(dev_t dev, __unused int flags, __unused int devtype, __unused proc_t p)
{
	int m = minor(dev);
	struct vopen *o = m >= 0 && m < MAX_OPENS ? opens[m] : NULL;

	if (o == NULL) {
		return 0;
	}
	lck_mtx_lock(&o->lock);
	for (int i = 0; i < MAX_RES; i++) {
		if (o->res[i].id) {
			res_free(o, &o->res[i]);
		}
	}
	vgpu3d_close(&o->g);
	if (o->g.pipe_va) {
		kmem_free(kernel_map, (vm_offset_t)o->g.pipe_va, round_page(o->g.pipe_size));
	}
	lck_mtx_unlock(&o->lock);
	lck_mtx_lock(&vgpu3d_opens_lock);
	opens[m] = NULL;
	lck_mtx_unlock(&vgpu3d_opens_lock);
	lck_mtx_destroy(&o->lock, &vgpu3d_lck_grp);
	IOFreeData(o, sizeof(*o));
	return 0;
}

static struct vopen *
open_of(dev_t dev)
{
	int m = minor(dev);
	struct vopen *o = m >= 0 && m < MAX_OPENS ? opens[m] : NULL;
	return o && o->ready ? o : NULL;
}

static int
vgpu3d_dev_write(dev_t dev, struct uio *uio, __unused int flag)
{
	struct vopen *o = open_of(dev);
	int err = 0;

	if (o == NULL) {
		return ENXIO;
	}
	lck_mtx_lock(&o->lock);
	while (uio_resid(uio) > 0 && !err) {
		uint32_t n = uio_resid(uio) > (user_ssize_t)o->g.pipe_size ? o->g.pipe_size : (uint32_t)uio_resid(uio);
		err = uiomove((caddr_t)o->g.pipe_va, (int)n, uio);
		if (!err) {
			o->g.wpos = n;
			if (vgpu3d_flush(&o->g)) {
				err = EIO;
			}
		}
	}
	lck_mtx_unlock(&o->lock);
	return err;
}

static int
vgpu3d_dev_read(dev_t dev, struct uio *uio, __unused int flag)
{
	struct vopen *o = open_of(dev);
	int err = 0;

	if (o == NULL) {
		return ENXIO;
	}
	lck_mtx_lock(&o->lock);
	while (uio_resid(uio) > 0 && !err) {
		uint32_t n = uio_resid(uio) > (user_ssize_t)o->g.pipe_size ? o->g.pipe_size : (uint32_t)uio_resid(uio);
		if (vgpu3d_transfer_box(&o->g, o->g.pipe_res, false, 0, 0, n, 1, 0)) {
			err = EIO;
			break;
		}
		o->g.bytes_in += n;
		err = uiomove((caddr_t)o->g.pipe_va, (int)n, uio);
	}
	lck_mtx_unlock(&o->lock);
	return err;
}

static int
vgpu3d_dev_ioctl(dev_t dev, u_long cmd, caddr_t data, __unused int flag, __unused proc_t p)
{
	struct vopen *o = open_of(dev);
	int err;

	if (o == NULL) {
		return ENXIO;
	}
	lck_mtx_lock(&o->lock);
	switch (cmd) {
	case VGPU3D_IOC_CREATE:
		err = res_create(o, (struct vgpu3d_ioc_create *)data);
		break;
	case VGPU3D_IOC_UNREF: {
		struct res *r = res_find(o, *(uint32_t *)data);
		err = r ? 0 : ENOENT;
		if (r) {
			res_free(o, r);
		}
		break;
	}
	case VGPU3D_IOC_UPLOAD:
		err = res_xfer(o, (struct vgpu3d_ioc_xfer *)data, true);
		break;
	case VGPU3D_IOC_DOWNLOAD:
		err = res_xfer(o, (struct vgpu3d_ioc_xfer *)data, false);
		break;
	default:
		err = ENOTTY;
		break;
	}
	lck_mtx_unlock(&o->lock);
	return err;
}

static const struct cdevsw vgpu3d_cdevsw = {
	.d_open = vgpu3d_dev_open, .d_close = vgpu3d_dev_close, .d_read = vgpu3d_dev_read,
	.d_write = vgpu3d_dev_write, .d_ioctl = vgpu3d_dev_ioctl, .d_stop = eno_stop, .d_reset = eno_reset,
	.d_select = eno_select, .d_mmap = eno_mmap, .d_strategy = eno_strat, .d_type = 0
};

/* ---- the boot self-test --------------------------------------------------------------- */

static void
vgpu3d_selftest_thread(__unused void *param, __unused wait_result_t wr)
{
	static struct vgpu3d g;
	static struct vgpu3d_selftest t;
	char size[24] = "";
	uint32_t w = 828, h = 1792;
	uint64_t pa;

	if (PE_parse_boot_argn("gpu3d_size", size, sizeof(size))) {
		char *x = strchr(size, 'x');
		if (x != NULL) {
			*x = 0;
			w = (uint32_t)strtoul(size, NULL, 10);
			h = (uint32_t)strtoul(x + 1, NULL, 10);
		}
	}
	lck_mtx_lock(&vgpu3d_opens_lock);
	int err = vgpu3d_probe();
	lck_mtx_unlock(&vgpu3d_opens_lock);
	g.ops = xnu_ops;
	g.features = vgpu3d_features;
	g.ctx_id = 1;
	if (err || vgpu3d_open(&g, vgpu3d_gles, PIPE_SIZE)) {
		printf("GPU3D-DONE fail\n");
		return;
	}
	t.width = w;
	t.height = h;
	t.pixels = xnu_dma_alloc(NULL, (size_t)w * h * 4, &pa);
	if (t.pixels == NULL) {
		printf("GPU3D-DONE fail\n");
		return;
	}
	uint64_t t0 = xnu_now_ns(NULL);
	err = vgpu3d_selftest(&g, &t);
	printf("GPU3D kernel selftest_ms=%llu\n", (xnu_now_ns(NULL) - t0) / 1000000);
	printf("GPU3D-DONE %s\n", !err && t.checks_passed == t.checks_total ? "pass" : "fail");
	vgpu3d_close(&g);
}

/* From bsd_init, after devfs: /dev/vgpu3d, and with boot-arg gpu3d=1 (or 2) the self-test. */
void
redwood_vgpu3d_init(void)
{
	uint32_t on = 0;
	thread_t th;

	vgpu3d_major = cdevsw_add(-1, &vgpu3d_cdevsw);
	if (vgpu3d_major < 0 || devfs_make_node_clone(makedev(vgpu3d_major, 0), DEVFS_CHAR, UID_ROOT, GID_WHEEL,
	    0666, vgpu3d_clone, "vgpu3d") == NULL) {
		printf("vgpu3d: no /dev/vgpu3d\n");
	}
	if (!PE_parse_boot_argn("gpu3d", &on, sizeof(on)) || !on) {
		return;
	}
	if (on == 1) {
		vgpu3d_selftest_thread(NULL, THREAD_AWAKENED);
	} else if (kernel_thread_start(vgpu3d_selftest_thread, NULL, &th) == KERN_SUCCESS) {
		thread_deallocate(th);
	}
}
