/*
 * virtio-gpu 3D through gfxstream: contexts, the pipe to a host render thread, the
 * renderControl / GLES 2 wire encoding, and a self-test that has the host GPU draw a picture
 * and send it back. See sys/redwood_vgpu3d.h for what this is and what it needs.
 *
 * Every call goes through g->ops; the first failure is kept in g->err, and every call after
 * it does nothing, so a sequence of calls can be checked once at its end.
 */
#include <stdarg.h>
#include <sys/redwood_vgpu3d.h>

#define GL_VENDOR               0x1F00
#define GL_RENDERER             0x1F01
#define GL_VERSION              0x1F02
#define GL_RGBA                 0x1908
#define GL_UNSIGNED_BYTE        0x1401
#define GL_FLOAT                0x1406
#define GL_TRIANGLES            0x0004
#define GL_COLOR_BUFFER_BIT     0x4000
#define GL_SCISSOR_TEST         0x0C11
#define GL_FRAGMENT_SHADER      0x8B30
#define GL_VERTEX_SHADER        0x8B31
#define GL_COMPILE_STATUS       0x8B81
#define GL_LINK_STATUS          0x8B82

#define EGL_ALPHA_SIZE          0x3021
#define EGL_BLUE_SIZE           0x3022
#define EGL_GREEN_SIZE          0x3023
#define EGL_RED_SIZE            0x3024
#define EGL_SURFACE_TYPE        0x3033
#define EGL_NONE                0x3038
#define EGL_RENDERABLE_TYPE     0x3040
#define EGL_PBUFFER_BIT         0x0001
#define EGL_WINDOW_BIT          0x0004
#define EGL_OPENGL_ES2_BIT      0x0004

#define GLES_API_2              2       /* gfxstream's GLESApi_2 */

/* renderControl opcodes (gfxstream guest/renderControl_enc/renderControl_opcodes.h) */
#define OP_rcGetRendererVersion         10000
#define OP_rcGetGLString                10003
#define OP_rcChooseConfig               10006
#define OP_rcCreateContext              10008
#define OP_rcCreateWindowSurface        10010
#define OP_rcCreateColorBuffer          10012
#define OP_rcSetWindowColorBuffer       10015
#define OP_rcFlushWindowColorBuffer     10016
#define OP_rcMakeCurrent                10017
#define OP_rcReadColorBuffer            10023

/* GLES 2 opcodes (gfxstream guest/GLESv2_enc/gl2_opcodes.h) */
#define OP_glAttachShader               2049
#define OP_glBindAttribLocation         2050
#define OP_glClear                      2063
#define OP_glClearColor                 2064
#define OP_glCompileShader              2068
#define OP_glCreateProgram              2073
#define OP_glCreateShader               2074
#define OP_glDisable                    2086
#define OP_glDrawArrays                 2088
#define OP_glEnable                     2090
#define OP_glEnableVertexAttribArray    2091
#define OP_glFinish                     2092
#define OP_glGetError                   2108
#define OP_glGetProgramiv               2112
#define OP_glGetShaderiv                2115
#define OP_glLinkProgram                2137
#define OP_glScissor                    2144
#define OP_glUseProgram                 2178
#define OP_glViewport                   2189
#define OP_glVertexAttribPointerData    2249
#define OP_glShaderString               2254
#define OP_glActiveTexture              2048
#define OP_glBindTexture                2054
#define OP_glBlendFunc                  2058
#define OP_glGenTextures                2101
#define OP_glTexImage2D                 2153
#define OP_glTexParameteri              2156

/* ---- small things the kernel and the freestanding probe both lack or name differently --- */

static void
v3_memcpy(void *dst, const void *src, size_t n)
{
	uint8_t *d = dst;
	const uint8_t *s = src;
	while (n--) {
		*d++ = *s++;
	}
}

static void
v3_memset(void *dst, int c, size_t n)
{
	uint8_t *d = dst;
	while (n--) {
		*d++ = (uint8_t)c;
	}
}

static size_t
v3_strlen(const char *s)
{
	size_t n = 0;
	while (s[n]) {
		n++;
	}
	return n;
}

struct fmtbuf { char *p; size_t left; };

static void
fmt_putc(struct fmtbuf *b, char c)
{
	if (b->left > 1) {
		*b->p++ = c;
		b->left--;
	}
}

static void
fmt_num(struct fmtbuf *b, uint64_t v, unsigned base, int width, char pad, bool neg)
{
	char tmp[24];
	int n = 0;
	do {
		tmp[n++] = "0123456789abcdef"[v % base];
		v /= base;
	} while (v && n < (int)sizeof(tmp));
	if (neg) {
		tmp[n++] = '-';
	}
	while (n < width) {
		fmt_putc(b, pad);
		width--;
	}
	while (n) {
		fmt_putc(b, tmp[--n]);
	}
}

static void
v3_vformat(char *out, size_t size, const char *f, va_list ap)
{
	struct fmtbuf b = { out, size };
	for (; *f; f++) {
		if (*f != '%') {
			fmt_putc(&b, *f);
			continue;
		}
		f++;
		char pad = ' ';
		int width = 0, lng = 0;
		if (*f == '0') {
			pad = '0';
			f++;
		}
		while (*f >= '0' && *f <= '9') {
			width = width * 10 + (*f++ - '0');
		}
		while (*f == 'l') {
			lng++;
			f++;
		}
		switch (*f) {
		case 's': {
			const char *s = va_arg(ap, const char *);
			if (!s) {
				s = "(null)";
			}
			while (*s) {
				fmt_putc(&b, *s++);
			}
			break;
		}
		case 'c':
			fmt_putc(&b, (char)va_arg(ap, int));
			break;
		case 'd': {
			int64_t v = lng ? va_arg(ap, int64_t) : va_arg(ap, int);
			fmt_num(&b, v < 0 ? (uint64_t)-v : (uint64_t)v, 10, width, pad, v < 0);
			break;
		}
		case 'u':
			fmt_num(&b, lng ? va_arg(ap, uint64_t) : va_arg(ap, unsigned), 10, width, pad, false);
			break;
		case 'x':
			fmt_num(&b, lng ? va_arg(ap, uint64_t) : va_arg(ap, unsigned), 16, width, pad, false);
			break;
		case 'p':
			fmt_putc(&b, '0');
			fmt_putc(&b, 'x');
			fmt_num(&b, (uintptr_t)va_arg(ap, void *), 16, 0, '0', false);
			break;
		case '%':
			fmt_putc(&b, '%');
			break;
		default:
			fmt_putc(&b, '?');
			break;
		}
	}
	*b.p = 0;
}

void
vgpu3d_log(struct vgpu3d *g, const char *fmt, ...)
{
	char line[256];
	va_list ap;
	va_start(ap, fmt);
	v3_vformat(line, sizeof(line), fmt, ap);
	va_end(ap);
	if (g->ops.log) {
		g->ops.log(g->ops.arg, line);
	}
}

static uint64_t
now_ns(struct vgpu3d *g)
{
	return g->ops.now_ns ? g->ops.now_ns(g->ops.arg) : 0;
}

static int
fail(struct vgpu3d *g, int err, const char *what)
{
	if (!g->err) {
		g->err = err;
		vgpu3d_log(g, "vgpu3d: %s failed (%d)", what, err);
	}
	return err;
}

/* ---- control queue ---------------------------------------------------------------------- */

static const char *
cmd_name(uint32_t type)
{
	switch (type) {
	case VIRTIO_GPU_CMD_GET_CAPSET_INFO: return "GET_CAPSET_INFO";
	case VIRTIO_GPU_CMD_GET_CAPSET: return "GET_CAPSET";
	case VIRTIO_GPU_CMD_CTX_CREATE: return "CTX_CREATE";
	case VIRTIO_GPU_CMD_CTX_ATTACH_RESOURCE: return "CTX_ATTACH_RESOURCE";
	case VIRTIO_GPU_CMD_RESOURCE_CREATE_3D: return "RESOURCE_CREATE_3D";
	case VIRTIO_GPU_CMD_RESOURCE_ATTACH_BACKING: return "RESOURCE_ATTACH_BACKING";
	case VIRTIO_GPU_CMD_TRANSFER_TO_HOST_3D: return "TRANSFER_TO_HOST_3D";
	case VIRTIO_GPU_CMD_TRANSFER_FROM_HOST_3D: return "TRANSFER_FROM_HOST_3D";
	case VIRTIO_GPU_CMD_SET_SCANOUT: return "SET_SCANOUT";
	case VIRTIO_GPU_CMD_RESOURCE_FLUSH: return "RESOURCE_FLUSH";
	default: return "command";
	}
}

/* One command; ok = the reply type expected. resp may be NULL for a plain header reply. */
static int
ctrl(struct vgpu3d *g, void *cmd, uint32_t len, void *resp, uint32_t resp_len, uint32_t ok)
{
	struct virtio_gpu_ctrl_hdr plain;
	struct virtio_gpu_ctrl_hdr *h = cmd;
	if (g->err) {
		return g->err;
	}
	if (!resp) {
		resp = &plain;
		resp_len = sizeof(plain);
	}
	v3_memset(resp, 0, resp_len);
	int r = g->ops.ctrl(g->ops.arg, cmd, len, resp, resp_len);
	if (r) {
		return fail(g, r, cmd_name(h->type));
	}
	uint32_t got = ((struct virtio_gpu_ctrl_hdr *)resp)->type;
	if (got != ok) {
		vgpu3d_log(g, "vgpu3d: %s (0x%x) answered 0x%x", cmd_name(h->type), h->type, got);
		return fail(g, 5 /* EIO */, cmd_name(h->type));
	}
	return 0;
}

static void
hdr(struct virtio_gpu_ctrl_hdr *h, uint32_t type, uint32_t ctx)
{
	v3_memset(h, 0, sizeof(*h));
	h->type = type;
	h->ctx_id = ctx;
}

bool
vgpu3d_has_capset(const struct vgpu3d *g, uint32_t id)
{
	for (uint32_t i = 0; i < g->num_capsets; i++) {
		if (g->capset[i].id == id) {
			return true;
		}
	}
	return false;
}

static const char *
capset_name(uint32_t id)
{
	switch (id) {
	case VIRTIO_GPU_CAPSET_VIRGL: return "virgl";
	case VIRTIO_GPU_CAPSET_VIRGL2: return "virgl2";
	case VIRTIO_GPU_CAPSET_GFXSTREAM_VULKAN: return "gfxstream-vulkan";
	case VIRTIO_GPU_CAPSET_VENUS: return "venus";
	case VIRTIO_GPU_CAPSET_CROSS_DOMAIN: return "cross-domain";
	case VIRTIO_GPU_CAPSET_DRM: return "drm";
	case VIRTIO_GPU_CAPSET_GFXSTREAM_MAGMA: return "gfxstream-magma";
	case VIRTIO_GPU_CAPSET_GFXSTREAM_GLES: return "gfxstream-gles";
	case VIRTIO_GPU_CAPSET_GFXSTREAM_COMPOSER: return "gfxstream-composer";
	default: return "?";
	}
}

int
vgpu3d_query(struct vgpu3d *g, uint64_t features, uint32_t num_capsets)
{
	g->features = features;
	g->num_capsets = 0;
	if (!g->next_resource) {
		g->next_resource = VGPU3D_FIRST_RESOURCE;
	}
	vgpu3d_log(g, "vgpu3d: features virgl=%d context_init=%d resource_blob=%d; %u capsets",
	    (int)(features >> VIRTIO_GPU_F_VIRGL & 1), (int)(features >> VIRTIO_GPU_F_CONTEXT_INIT & 1),
	    (int)(features >> VIRTIO_GPU_F_RESOURCE_BLOB & 1), num_capsets);
	if (!(features & (1ull << VIRTIO_GPU_F_VIRGL))) {
		vgpu3d_log(g, "vgpu3d: the device has no 3D (VIRTIO_GPU_F_VIRGL not offered)");
		return fail(g, 45 /* ENOTSUP */, "3D");
	}
	for (uint32_t i = 0; i < num_capsets && i < VGPU3D_MAX_CAPSETS; i++) {
		struct virtio_gpu_get_capset_info c;
		struct virtio_gpu_resp_capset_info r;
		v3_memset(&c, 0, sizeof(c));
		hdr(&c.hdr, VIRTIO_GPU_CMD_GET_CAPSET_INFO, 0);
		c.capset_index = i;
		if (num_capsets == VGPU3D_PROBE_CAPSETS) {
			/* the count is unknown: ask until the device says no */
			v3_memset(&r, 0, sizeof(r));
			if (g->ops.ctrl(g->ops.arg, &c, sizeof(c), &r, sizeof(r)) != 0 ||
			    r.hdr.type != VIRTIO_GPU_RESP_OK_CAPSET_INFO || r.capset_id == 0 ||
			    r.capset_id > 64 /* crosvm poisons the answer past the last one */) {
				break;
			}
		} else if (ctrl(g, &c, sizeof(c), &r, sizeof(r), VIRTIO_GPU_RESP_OK_CAPSET_INFO)) {
			return g->err;
		}
		g->capset[i].id = r.capset_id;
		g->capset[i].max_version = r.capset_max_version;
		g->capset[i].max_size = r.capset_max_size;
		g->num_capsets++;
		vgpu3d_log(g, "vgpu3d: capset %u: id %u (%s) version %u size %u", i, r.capset_id,
		    capset_name(r.capset_id), r.capset_max_version, r.capset_max_size);
	}
	if (vgpu3d_has_capset(g, VIRTIO_GPU_CAPSET_GFXSTREAM_GLES)) {
		struct {
			struct virtio_gpu_ctrl_hdr hdr;
			uint32_t data[16];
		} r;
		struct virtio_gpu_get_capset c;
		v3_memset(&c, 0, sizeof(c));
		hdr(&c.hdr, VIRTIO_GPU_CMD_GET_CAPSET, 0);
		c.capset_id = VIRTIO_GPU_CAPSET_GFXSTREAM_GLES;
		c.capset_version = 0;
		if (ctrl(g, &c, sizeof(c), &r, sizeof(r), VIRTIO_GPU_RESP_OK_CAPSET) == 0) {
			v3_memcpy(g->gles_capset, r.data, sizeof(g->gles_capset));
			vgpu3d_log(g, "vgpu3d: gfxstream-gles capset: protocol %u ring %u buffer %u align %u",
			    r.data[0], r.data[1], r.data[2], r.data[3]);
		}
	}
	return g->err;
}

static int
attach_backing(struct vgpu3d *g, uint32_t res, uint64_t pa, uint32_t len)
{
	struct virtio_gpu_resource_attach_backing a;
	v3_memset(&a, 0, sizeof(a));
	hdr(&a.hdr, VIRTIO_GPU_CMD_RESOURCE_ATTACH_BACKING, 0);
	a.resource_id = res;
	a.nr_entries = 1;
	a.entry[0].addr = pa;
	a.entry[0].length = len;
	return ctrl(g, &a, sizeof(a), NULL, 0, VIRTIO_GPU_RESP_OK_NODATA);
}

static int
ctx_attach(struct vgpu3d *g, uint32_t res)
{
	struct virtio_gpu_ctx_resource c;
	v3_memset(&c, 0, sizeof(c));
	hdr(&c.hdr, VIRTIO_GPU_CMD_CTX_ATTACH_RESOURCE, g->ctx_id);
	c.resource_id = res;
	return ctrl(g, &c, sizeof(c), NULL, 0, VIRTIO_GPU_RESP_OK_NODATA);
}

static int
create_3d(struct vgpu3d *g, uint32_t res, uint32_t target, uint32_t format, uint32_t bind,
    uint32_t w, uint32_t h)
{
	struct virtio_gpu_resource_create_3d c;
	v3_memset(&c, 0, sizeof(c));
	hdr(&c.hdr, VIRTIO_GPU_CMD_RESOURCE_CREATE_3D, g->ctx_id);
	c.resource_id = res;
	c.target = target;
	c.format = format;
	c.bind = bind;
	c.width = w;
	c.height = h;
	c.depth = 1;
	c.array_size = 1;
	return ctrl(g, &c, sizeof(c), NULL, 0, VIRTIO_GPU_RESP_OK_NODATA);
}

static int
transfer(struct vgpu3d *g, bool to_host, uint32_t len)
{
	struct virtio_gpu_transfer_host_3d t;
	v3_memset(&t, 0, sizeof(t));
	hdr(&t.hdr, to_host ? VIRTIO_GPU_CMD_TRANSFER_TO_HOST_3D : VIRTIO_GPU_CMD_TRANSFER_FROM_HOST_3D,
	    g->ctx_id);
	t.box.w = len;
	t.box.h = 1;
	t.box.d = 1;
	t.resource_id = g->pipe_res;
	g->transfers++;
	return ctrl(g, &t, sizeof(t), NULL, 0, VIRTIO_GPU_RESP_OK_NODATA);
}

int
vgpu3d_open(struct vgpu3d *g, uint32_t capset, uint32_t pipe_size)
{
	if (g->err) {
		return g->err;
	}
	if (!g->next_resource) {
		g->next_resource = VGPU3D_FIRST_RESOURCE;
	}
	if (!(g->features & (1ull << VIRTIO_GPU_F_CONTEXT_INIT))) {
		capset = 0;     /* the host's default context type */
	}

	struct virtio_gpu_ctx_create c;
	static const char name[] = "vgpu3d";
	v3_memset(&c, 0, sizeof(c));
	if (!g->ctx_id) {
		g->ctx_id = 1;
	}
	hdr(&c.hdr, VIRTIO_GPU_CMD_CTX_CREATE, g->ctx_id);
	c.nlen = sizeof(name) - 1;
	c.context_init = capset;
	v3_memcpy(c.debug_name, name, sizeof(name) - 1);
	if (ctrl(g, &c, sizeof(c), NULL, 0, VIRTIO_GPU_RESP_OK_NODATA)) {
		g->ctx_id = 0;
		return g->err;
	}
	g->ctx_capset = capset;
	vgpu3d_log(g, "vgpu3d: context %u created (capset %u %s)", g->ctx_id, capset,
	    capset ? capset_name(capset) : "default");

	g->pipe_size = pipe_size;
	g->pipe_va = g->ops.dma_alloc(g->ops.arg, pipe_size, &g->pipe_pa);
	if (!g->pipe_va) {
		return fail(g, 12 /* ENOMEM */, "pipe buffer");
	}
	g->pipe_res = g->next_resource++;
	if (create_3d(g, g->pipe_res, VGPU3D_PIPE_BUFFER, VIRGL_FORMAT_R8_UNORM, VIRGL_BIND_CUSTOM,
	    pipe_size, 1) || attach_backing(g, g->pipe_res, g->pipe_pa, pipe_size) ||
	    ctx_attach(g, g->pipe_res)) {
		return g->err;
	}

	/* the service, then the client flags word the render thread reads first (unused, 0) */
	static const char service[] = "pipe:opengles";
	v3_memcpy(g->pipe_va, service, sizeof(service));
	if (transfer(g, true, sizeof(service))) {
		return g->err;
	}
	uint32_t *flags = (uint32_t *)vgpu3d_alloc(g, 4);
	if (flags) {
		*flags = 0;
	}
	vgpu3d_flush(g);
	if (!g->err) {
		vgpu3d_log(g, "vgpu3d: pipe resource %u (%u KiB at %p) connected to %s", g->pipe_res,
		    pipe_size >> 10, (void *)(uintptr_t)g->pipe_pa, service);
	}
	return g->err;
}

uint32_t
vgpu3d_create_scanout_resource(struct vgpu3d *g, uint32_t width, uint32_t height)
{
	uint32_t res = g->next_resource++;
	uint64_t pa;
	size_t size = (size_t)width * height * 4;
	void *va = g->ops.dma_alloc(g->ops.arg, size, &pa);
	if (!va) {
		fail(g, 12, "scanout buffer");
		return 0;
	}
	if (create_3d(g, res, VGPU3D_PIPE_TEXTURE_2D, VIRGL_FORMAT_R8G8B8A8_UNORM,
	    VIRGL_BIND_RENDER_TARGET | VIRGL_BIND_SAMPLER_VIEW | VIRGL_BIND_SCANOUT, width, height) ||
	    attach_backing(g, res, pa, (uint32_t)size) || ctx_attach(g, res)) {
		return 0;
	}
	return res;
}

int
vgpu3d_present(struct vgpu3d *g, uint32_t scanout, uint32_t res, uint32_t width, uint32_t height)
{
	struct virtio_gpu_set_scanout s;
	struct virtio_gpu_resource_flush f;
	v3_memset(&s, 0, sizeof(s));
	hdr(&s.hdr, VIRTIO_GPU_CMD_SET_SCANOUT, 0);
	s.r.width = width;
	s.r.height = height;
	s.scanout_id = scanout;
	s.resource_id = res;
	v3_memset(&f, 0, sizeof(f));
	hdr(&f.hdr, VIRTIO_GPU_CMD_RESOURCE_FLUSH, 0);
	f.r.width = width;
	f.r.height = height;
	f.resource_id = res;
	if (ctrl(g, &s, sizeof(s), NULL, 0, VIRTIO_GPU_RESP_OK_NODATA)) {
		return g->err;
	}
	return ctrl(g, &f, sizeof(f), NULL, 0, VIRTIO_GPU_RESP_OK_NODATA);
}

int
vgpu3d_resource_create(struct vgpu3d *g, uint32_t res, uint32_t width, uint32_t height, uint32_t format,
    uint32_t bind, const struct virtio_gpu_mem_entry *entries, uint32_t nentries, void *scratch, uint32_t scratch_size)
{
	struct virtio_gpu_resource_attach_backing *a = scratch;
	uint32_t len = (uint32_t)(sizeof(*a) - sizeof(a->entry) + nentries * sizeof(a->entry[0]));
	if (len > scratch_size) {
		return 7;       /* E2BIG */
	}
	if (create_3d(g, res, VGPU3D_PIPE_TEXTURE_2D, format, bind, width, height)) {
		return g->err;
	}
	v3_memset(a, 0, sizeof(*a) - sizeof(a->entry));
	hdr(&a->hdr, VIRTIO_GPU_CMD_RESOURCE_ATTACH_BACKING, 0);
	a->resource_id = res;
	a->nr_entries = nentries;
	v3_memcpy(a->entry, entries, nentries * sizeof(a->entry[0]));
	if (ctrl(g, a, len, NULL, 0, VIRTIO_GPU_RESP_OK_NODATA)) {
		return g->err;
	}
	return ctx_attach(g, res);
}

int
vgpu3d_resource_unref(struct vgpu3d *g, uint32_t res)
{
	struct virtio_gpu_ctx_resource d;
	struct virtio_gpu_resource_unref u;
	v3_memset(&d, 0, sizeof(d));
	hdr(&d.hdr, VIRTIO_GPU_CMD_CTX_DETACH_RESOURCE, g->ctx_id);
	d.resource_id = res;
	v3_memset(&u, 0, sizeof(u));
	hdr(&u.hdr, VIRTIO_GPU_CMD_RESOURCE_UNREF, 0);
	u.resource_id = res;
	ctrl(g, &d, sizeof(d), NULL, 0, VIRTIO_GPU_RESP_OK_NODATA);
	return ctrl(g, &u, sizeof(u), NULL, 0, VIRTIO_GPU_RESP_OK_NODATA);
}

int
vgpu3d_transfer_box(struct vgpu3d *g, uint32_t res, bool to_host, uint32_t x, uint32_t y, uint32_t w, uint32_t h,
    uint64_t offset)
{
	struct virtio_gpu_transfer_host_3d t;
	v3_memset(&t, 0, sizeof(t));
	hdr(&t.hdr, to_host ? VIRTIO_GPU_CMD_TRANSFER_TO_HOST_3D : VIRTIO_GPU_CMD_TRANSFER_FROM_HOST_3D, g->ctx_id);
	t.box.x = x;
	t.box.y = y;
	t.box.w = w;
	t.box.h = h;
	t.box.d = 1;
	t.offset = offset;
	t.resource_id = res;
	return ctrl(g, &t, sizeof(t), NULL, 0, VIRTIO_GPU_RESP_OK_NODATA);
}

void
vgpu3d_close(struct vgpu3d *g)
{
	struct virtio_gpu_ctrl_hdr d;
	if (g->pipe_res) {
		g->err = 0;
		vgpu3d_resource_unref(g, g->pipe_res);
		g->pipe_res = 0;
	}
	if (g->ctx_id) {
		g->err = 0;
		hdr(&d, VIRTIO_GPU_CMD_CTX_DESTROY, g->ctx_id);
		ctrl(g, &d, sizeof(d), NULL, 0, VIRTIO_GPU_RESP_OK_NODATA);
	}
}

/* ---- the stream ------------------------------------------------------------------------- */

int
vgpu3d_flush(struct vgpu3d *g)
{
	if (g->err || !g->wpos) {
		return g->err;
	}
	uint32_t n = g->wpos;
	g->wpos = 0;
	g->bytes_out += n;
	return transfer(g, true, n);
}

uint8_t *
vgpu3d_alloc(struct vgpu3d *g, uint32_t n)
{
	if (g->err) {
		return NULL;
	}
	if (n > g->pipe_size) {
		fail(g, 7 /* E2BIG */, "encode");
		return NULL;
	}
	if (g->wpos + n > g->pipe_size && vgpu3d_flush(g)) {
		return NULL;
	}
	uint8_t *p = g->pipe_va + g->wpos;
	g->wpos += n;
	return p;
}

int
vgpu3d_read(struct vgpu3d *g, void *buf, uint32_t n)
{
	uint8_t *out = buf;
	if (vgpu3d_flush(g)) {
		return g->err;
	}
	while (n && !g->err) {
		uint32_t k = n < g->pipe_size ? n : g->pipe_size;
		if (transfer(g, false, k)) {
			break;
		}
		v3_memcpy(out, g->pipe_va, k);
		out += k;
		n -= k;
		g->bytes_in += k;
	}
	return g->err;
}

/* ---- encoding --------------------------------------------------------------------------- */

struct enc { uint8_t *p; };

static bool
begin(struct vgpu3d *g, struct enc *e, uint32_t op, uint32_t size)
{
	e->p = vgpu3d_alloc(g, size);
	if (!e->p) {
		return false;
	}
	v3_memcpy(e->p, &op, 4);
	v3_memcpy(e->p + 4, &size, 4);
	e->p += 8;
	return true;
}

static void
put32(struct enc *e, uint32_t v)
{
	v3_memcpy(e->p, &v, 4);
	e->p += 4;
}

static void
put8(struct enc *e, uint8_t v)
{
	*e->p++ = v;
}

static void
putbytes(struct enc *e, const void *d, uint32_t n)
{
	v3_memcpy(e->p, d, n);
	e->p += n;
}

static uint32_t
read32(struct vgpu3d *g)
{
	uint32_t v = 0;
	vgpu3d_read(g, &v, 4);
	return v;
}

/* calls of only 32-bit arguments */
static bool
call(struct vgpu3d *g, uint32_t op, int nargs, const uint32_t *args)
{
	struct enc e;
	if (!begin(g, &e, op, 8 + 4 * (uint32_t)nargs)) {
		return false;
	}
	for (int i = 0; i < nargs; i++) {
		put32(&e, args[i]);
	}
	return true;
}

#define CALL(g, op, ...) ({ const uint32_t _a[] = { __VA_ARGS__ }; \
	call(g, op, (int)(sizeof(_a) / sizeof(_a[0])), _a); })

uint32_t
rc_get_renderer_version(struct vgpu3d *g)
{
	return call(g, OP_rcGetRendererVersion, 0, NULL) ? read32(g) : 0;
}

int32_t
rc_get_gl_string(struct vgpu3d *g, uint32_t name, char *buf, uint32_t size)
{
	if (!CALL(g, OP_rcGetGLString, name, size /* out-buffer size */, size /* bufferSize */)) {
		return -1;
	}
	vgpu3d_read(g, buf, size);
	buf[size - 1] = 0;
	return (int32_t)read32(g);
}

int32_t
rc_choose_config(struct vgpu3d *g, const int32_t *attribs, uint32_t attribs_bytes, uint32_t *configs,
    uint32_t nconfigs)
{
	struct enc e;
	if (!begin(g, &e, OP_rcChooseConfig, 8 + 4 + attribs_bytes + 4 + 4 + 4)) {
		return -1;
	}
	put32(&e, attribs_bytes);
	putbytes(&e, attribs, attribs_bytes);
	put32(&e, attribs_bytes);
	put32(&e, nconfigs * 4);
	put32(&e, nconfigs);
	vgpu3d_read(g, configs, nconfigs * 4);
	return (int32_t)read32(g);
}

uint32_t
rc_create_context(struct vgpu3d *g, uint32_t config, uint32_t share, uint32_t gles_api)
{
	return CALL(g, OP_rcCreateContext, config, share, gles_api) ? read32(g) : 0;
}

uint32_t
rc_create_window_surface(struct vgpu3d *g, uint32_t config, uint32_t w, uint32_t h)
{
	return CALL(g, OP_rcCreateWindowSurface, config, w, h) ? read32(g) : 0;
}

uint32_t
rc_create_color_buffer(struct vgpu3d *g, uint32_t w, uint32_t h, uint32_t internal_format)
{
	return CALL(g, OP_rcCreateColorBuffer, w, h, internal_format) ? read32(g) : 0;
}

void
rc_set_window_color_buffer(struct vgpu3d *g, uint32_t surface, uint32_t color_buffer)
{
	CALL(g, OP_rcSetWindowColorBuffer, surface, color_buffer);
}

int32_t
rc_make_current(struct vgpu3d *g, uint32_t ctx, uint32_t draw, uint32_t read)
{
	return CALL(g, OP_rcMakeCurrent, ctx, draw, read) ? (int32_t)read32(g) : 0;
}

int32_t
rc_flush_window_color_buffer(struct vgpu3d *g, uint32_t surface)
{
	return CALL(g, OP_rcFlushWindowColorBuffer, surface) ? (int32_t)read32(g) : -1;
}

int
rc_read_color_buffer(struct vgpu3d *g, uint32_t cb, uint32_t x, uint32_t y, uint32_t w, uint32_t h,
    void *pixels)
{
	uint32_t size = w * h * 4;
	if (!CALL(g, OP_rcReadColorBuffer, cb, x, y, w, h, GL_RGBA, GL_UNSIGNED_BYTE, size)) {
		return g->err;
	}
	return vgpu3d_read(g, pixels, size);
}

void gl_viewport(struct vgpu3d *g, int32_t x, int32_t y, int32_t w, int32_t h)
{ CALL(g, OP_glViewport, (uint32_t)x, (uint32_t)y, (uint32_t)w, (uint32_t)h); }
void gl_clear_color(struct vgpu3d *g, uint32_t r, uint32_t gr, uint32_t b, uint32_t a)
{ CALL(g, OP_glClearColor, r, gr, b, a); }
void gl_clear(struct vgpu3d *g, uint32_t mask) { CALL(g, OP_glClear, mask); }
void gl_enable(struct vgpu3d *g, uint32_t cap) { CALL(g, OP_glEnable, cap); }
void gl_disable(struct vgpu3d *g, uint32_t cap) { CALL(g, OP_glDisable, cap); }
void gl_scissor(struct vgpu3d *g, int32_t x, int32_t y, int32_t w, int32_t h)
{ CALL(g, OP_glScissor, (uint32_t)x, (uint32_t)y, (uint32_t)w, (uint32_t)h); }
void gl_finish(struct vgpu3d *g) { call(g, OP_glFinish, 0, NULL); }
uint32_t gl_get_error(struct vgpu3d *g) { return call(g, OP_glGetError, 0, NULL) ? read32(g) : 0; }
uint32_t gl_create_shader(struct vgpu3d *g, uint32_t type)
{ return CALL(g, OP_glCreateShader, type) ? read32(g) : 0; }
void gl_compile_shader(struct vgpu3d *g, uint32_t shader) { CALL(g, OP_glCompileShader, shader); }
uint32_t gl_create_program(struct vgpu3d *g) { return call(g, OP_glCreateProgram, 0, NULL) ? read32(g) : 0; }
void gl_attach_shader(struct vgpu3d *g, uint32_t program, uint32_t shader)
{ CALL(g, OP_glAttachShader, program, shader); }
void gl_link_program(struct vgpu3d *g, uint32_t program) { CALL(g, OP_glLinkProgram, program); }
void gl_use_program(struct vgpu3d *g, uint32_t program) { CALL(g, OP_glUseProgram, program); }
void gl_enable_vertex_attrib_array(struct vgpu3d *g, uint32_t index)
{ CALL(g, OP_glEnableVertexAttribArray, index); }
void gl_draw_arrays(struct vgpu3d *g, uint32_t mode, int32_t first, int32_t count)
{ CALL(g, OP_glDrawArrays, mode, (uint32_t)first, (uint32_t)count); }

void gl_bind_texture(struct vgpu3d *g, uint32_t target, uint32_t texture)
{ CALL(g, OP_glBindTexture, target, texture); }
void gl_tex_parameteri(struct vgpu3d *g, uint32_t target, uint32_t pname, int32_t param)
{ CALL(g, OP_glTexParameteri, target, pname, (uint32_t)param); }
void gl_blend_func(struct vgpu3d *g, uint32_t sfactor, uint32_t dfactor)
{ CALL(g, OP_glBlendFunc, sfactor, dfactor); }
void gl_active_texture(struct vgpu3d *g, uint32_t unit) { CALL(g, OP_glActiveTexture, unit); }

void
gl_gen_textures(struct vgpu3d *g, uint32_t n, uint32_t *textures)
{
	if (CALL(g, OP_glGenTextures, n, n * 4)) {
		vgpu3d_read(g, textures, n * 4);
	}
}

void
gl_tex_image_2d_rgba(struct vgpu3d *g, uint32_t target, int32_t level, int32_t w, int32_t h,
    const void *pixels)
{
	/* the header, then the pixel count and the pixels as one more stretch of the stream */
	uint32_t size = (uint32_t)w * (uint32_t)h * 4;
	struct enc e;
	if (!begin(g, &e, OP_glTexImage2D, 8 + 4 * 8 + 4 + size)) {
		if (!g->err) {
			fail(g, 7, "glTexImage2D");
		}
		return;
	}
	put32(&e, target);
	put32(&e, (uint32_t)level);
	put32(&e, GL_RGBA);
	put32(&e, (uint32_t)w);
	put32(&e, (uint32_t)h);
	put32(&e, 0);
	put32(&e, GL_RGBA);
	put32(&e, GL_UNSIGNED_BYTE);
	put32(&e, size);
	putbytes(&e, pixels, size);
}

int32_t
gl_get_shaderiv(struct vgpu3d *g, uint32_t shader, uint32_t pname)
{
	return CALL(g, OP_glGetShaderiv, shader, pname, 4) ? (int32_t)read32(g) : 0;
}

int32_t
gl_get_programiv(struct vgpu3d *g, uint32_t program, uint32_t pname)
{
	return CALL(g, OP_glGetProgramiv, program, pname, 4) ? (int32_t)read32(g) : 0;
}

void
gl_shader_string(struct vgpu3d *g, uint32_t shader, const char *src)
{
	struct enc e;
	uint32_t len = (uint32_t)v3_strlen(src) + 1;
	if (!begin(g, &e, OP_glShaderString, 8 + 4 + 4 + len + 4)) {
		return;
	}
	put32(&e, shader);
	put32(&e, len);
	putbytes(&e, src, len);
	put32(&e, len);
}

void
gl_bind_attrib_location(struct vgpu3d *g, uint32_t program, uint32_t index, const char *name)
{
	struct enc e;
	uint32_t len = (uint32_t)v3_strlen(name) + 1;
	if (!begin(g, &e, OP_glBindAttribLocation, 8 + 4 + 4 + 4 + len)) {
		return;
	}
	put32(&e, program);
	put32(&e, index);
	put32(&e, len);
	putbytes(&e, name, len);
}

void
gl_vertex_attrib_pointer_data(struct vgpu3d *g, uint32_t index, int32_t size, uint32_t type,
    bool normalized, int32_t stride, const void *data, uint32_t len)
{
	struct enc e;
	if (!begin(g, &e, OP_glVertexAttribPointerData, 8 + 4 + 4 + 4 + 1 + 4 + 4 + len + 4)) {
		return;
	}
	put32(&e, index);
	put32(&e, (uint32_t)size);
	put32(&e, type);
	put8(&e, normalized);
	put32(&e, (uint32_t)stride);
	put32(&e, len);
	putbytes(&e, data, len);
	put32(&e, len);
}

/* ---- the self-test ---------------------------------------------------------------------- */

static const char vs_src[] =
	"attribute vec2 pos;\n"
	"attribute vec3 col;\n"
	"varying vec3 v;\n"
	"void main() { v = col; gl_Position = vec4(pos, 0.0, 1.0); }\n";
static const char fs_src[] =
	"precision mediump float;\n"
	"varying vec3 v;\n"
	"void main() { gl_FragColor = vec4(v, 1.0); }\n";

static const uint32_t tri_pos[6] = {
	VGPU3D_F_M0_75, VGPU3D_F_M0_75, VGPU3D_F_0_75, VGPU3D_F_M0_75, VGPU3D_F_0, VGPU3D_F_0_75,
};
static const uint32_t tri_col[9] = {
	VGPU3D_F_1, VGPU3D_F_0, VGPU3D_F_0, VGPU3D_F_0, VGPU3D_F_1, VGPU3D_F_0, VGPU3D_F_0, VGPU3D_F_0, VGPU3D_F_1,
};

/* clear colours of the frames: a gradient of the background's blue */
static const uint32_t bg_blue[4] = { VGPU3D_F_0_2, VGPU3D_F_0_25, VGPU3D_F_0_5, VGPU3D_F_0_75 };

static void
draw_frame(struct vgpu3d *g, uint32_t w, uint32_t h, uint32_t program, uint32_t blue)
{
	gl_viewport(g, 0, 0, (int32_t)w, (int32_t)h);
	gl_disable(g, GL_SCISSOR_TEST);
	gl_clear_color(g, VGPU3D_F_0_1, VGPU3D_F_0_1, blue, VGPU3D_F_1);
	gl_clear(g, GL_COLOR_BUFFER_BIT);
	/* three bars along the top eighth: red, green, blue */
	static const uint32_t bars[3][3] = {
		{ VGPU3D_F_1, VGPU3D_F_0, VGPU3D_F_0 },
		{ VGPU3D_F_0, VGPU3D_F_1, VGPU3D_F_0 },
		{ VGPU3D_F_0, VGPU3D_F_0, VGPU3D_F_1 },
	};
	gl_enable(g, GL_SCISSOR_TEST);
	for (uint32_t i = 0; i < 3; i++) {
		gl_scissor(g, (int32_t)(w * i / 3), (int32_t)(h - h / 8), (int32_t)(w / 3), (int32_t)(h / 8));
		gl_clear_color(g, bars[i][0], bars[i][1], bars[i][2], VGPU3D_F_1);
		gl_clear(g, GL_COLOR_BUFFER_BIT);
	}
	gl_disable(g, GL_SCISSOR_TEST);
	/* a triangle shaded on the host: one primary colour per corner */
	gl_use_program(g, program);
	gl_vertex_attrib_pointer_data(g, 0, 2, GL_FLOAT, false, 0, tri_pos, sizeof(tri_pos));
	gl_enable_vertex_attrib_array(g, 0);
	gl_vertex_attrib_pointer_data(g, 1, 3, GL_FLOAT, false, 0, tri_col, sizeof(tri_col));
	gl_enable_vertex_attrib_array(g, 1);
	gl_draw_arrays(g, GL_TRIANGLES, 0, 3);
}

static const uint8_t *
px(const struct vgpu3d_selftest *t, uint32_t x, uint32_t y_gl, bool flipped)
{
	uint32_t row = flipped ? t->height - 1 - y_gl : y_gl;
	return t->pixels + ((size_t)row * t->width + x) * 4;
}

static int
near3(int a, int b)
{
	int d = a - b;
	return d >= -3 && d <= 3;
}

static uint32_t
run_checks(const struct vgpu3d_selftest *t, bool flipped, uint32_t *total)
{
	uint32_t w = t->width, h = t->height, ok = 0;
	const uint8_t *p;
	uint32_t bar_y = h - h / 16;
	*total = 5;
	p = px(t, 2, 2, flipped);               /* background: 0.1, 0.1, 0.2 */
	ok += near3(p[0], 26) && near3(p[1], 26) && near3(p[2], 51) && p[3] == 255;
	p = px(t, w / 6, bar_y, flipped);
	ok += p[0] == 255 && p[1] == 0 && p[2] == 0;
	p = px(t, w / 2, bar_y, flipped);
	ok += p[0] == 0 && p[1] == 255 && p[2] == 0;
	p = px(t, w * 5 / 6, bar_y, flipped);
	ok += p[0] == 0 && p[1] == 0 && p[2] == 255;
	/* the triangle's centroid, (0, -0.25) in clip space: all three corners' colours mixed */
	p = px(t, w / 2, h * 3 / 8, flipped);
	ok += p[0] > 50 && p[0] < 210 && p[1] > 50 && p[1] < 210 && p[2] > 50 && p[2] < 210;
	return ok;
}

static char
classify(const uint8_t *p)
{
	int r = p[0], gr = p[1], b = p[2];
	if (r > 200 && gr < 40 && b < 40) return 'R';
	if (gr > 200 && r < 40 && b < 40) return 'G';
	if (b > 200 && r < 40 && gr < 40) return 'B';
	if (r < 40 && gr < 40 && b < 70) return '.';
	if (r > 200 && gr > 200 && b > 200) return 'W';
	if (r >= gr && r >= b) return 'r';
	if (gr >= r && gr >= b) return 'g';
	return 'b';
}

int
vgpu3d_selftest(struct vgpu3d *g, struct vgpu3d_selftest *t)
{
	uint32_t w = t->width, h = t->height;
	uint64_t t0;

	t->renderer_version = rc_get_renderer_version(g);
	if (g->err) {
		return g->err;
	}
	vgpu3d_log(g, "GPU3D renderer_version=%u", t->renderer_version);

	static const int32_t attribs[] = {
		EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8,
		EGL_SURFACE_TYPE, EGL_WINDOW_BIT | EGL_PBUFFER_BIT,
		EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT, EGL_NONE,
	};
	uint32_t configs[8] = { 0 };
	int32_t n = rc_choose_config(g, attribs, sizeof(attribs), configs, 8);
	vgpu3d_log(g, "vgpu3d: rcChooseConfig: %d configs, first %u", n, configs[0]);
	if (n <= 0) {
		return fail(g, 2 /* ENOENT */, "rcChooseConfig");
	}
	uint32_t ctx = rc_create_context(g, configs[0], 0, GLES_API_2);
	uint32_t surf = rc_create_window_surface(g, configs[0], w, h);
	uint32_t cb = t->scanout_res ? t->scanout_res : rc_create_color_buffer(g, w, h, GL_RGBA);
	vgpu3d_log(g, "vgpu3d: context %u, window surface %u, colour buffer %u (%ux%u)", ctx, surf, cb, w, h);
	if (!ctx || !surf || !cb) {
		return fail(g, 5, "context/surface/colour buffer");
	}
	t->context = ctx;
	t->surface = surf;
	t->color_buffer = cb;
	rc_set_window_color_buffer(g, surf, cb);
	int32_t mc = rc_make_current(g, ctx, surf, surf);
	if (mc != 1) {
		return fail(g, 5, "rcMakeCurrent");
	}
	/* the host's strings need a current context */
	rc_get_gl_string(g, GL_VENDOR, t->vendor, sizeof(t->vendor));
	rc_get_gl_string(g, GL_RENDERER, t->renderer, sizeof(t->renderer));
	rc_get_gl_string(g, GL_VERSION, t->version, sizeof(t->version));
	vgpu3d_log(g, "GPU3D host_gl_vendor=%s", t->vendor);
	vgpu3d_log(g, "GPU3D host_gl_renderer=%s", t->renderer);
	vgpu3d_log(g, "GPU3D host_gl_version=%s", t->version);

	uint32_t vs = gl_create_shader(g, GL_VERTEX_SHADER);
	uint32_t fs = gl_create_shader(g, GL_FRAGMENT_SHADER);
	gl_shader_string(g, vs, vs_src);
	gl_compile_shader(g, vs);
	gl_shader_string(g, fs, fs_src);
	gl_compile_shader(g, fs);
	int32_t vs_ok = gl_get_shaderiv(g, vs, GL_COMPILE_STATUS);
	int32_t fs_ok = gl_get_shaderiv(g, fs, GL_COMPILE_STATUS);
	uint32_t prog = gl_create_program(g);
	gl_attach_shader(g, prog, vs);
	gl_attach_shader(g, prog, fs);
	gl_bind_attrib_location(g, prog, 0, "pos");
	gl_bind_attrib_location(g, prog, 1, "col");
	gl_link_program(g, prog);
	int32_t link_ok = gl_get_programiv(g, prog, GL_LINK_STATUS);
	vgpu3d_log(g, "GPU3D shaders vs=%u compiled=%d fs=%u compiled=%d program=%u linked=%d",
	    vs, vs_ok, fs, fs_ok, prog, link_ok);

	draw_frame(g, w, h, prog, VGPU3D_F_0_2);
	uint32_t glerr = gl_get_error(g);
	rc_flush_window_color_buffer(g, surf);
	t0 = now_ns(g);
	rc_read_color_buffer(g, cb, 0, 0, w, h, t->pixels);
	t->readback_ns = now_ns(g) - t0;
	if (g->err) {
		return g->err;
	}

	uint32_t total, a = run_checks(t, false, &total), b = run_checks(t, true, &total);
	bool flipped = b > a;
	t->rows_top_down = flipped;
	t->checks_passed = (flipped ? b : a) + (glerr == 0) + (link_ok == 1);
	t->checks_total = total + 2;
	uint32_t fnv = 2166136261u;
	for (size_t i = 0; i < (size_t)w * h * 4; i++) {
		fnv = (fnv ^ t->pixels[i]) * 16777619u;
	}
	t->fnv = fnv;
	vgpu3d_log(g, "GPU3D readback %ux%u rgba fnv1a=%08x gl_error=0x%x rows=%s", w, h, fnv, glerr,
	    flipped ? "top-down" : "bottom-up");
	vgpu3d_log(g, "GPU3D picture (what the host GPU drew, read back into the guest):");
	char line[72];
	for (uint32_t r = 0; r < 24; r++) {
		uint32_t y = h - 1 - (r * h + h / 2) / 24;  /* GL y, top first */
		uint32_t c;
		for (c = 0; c < 48; c++) {
			line[c] = classify(px(t, (c * w + w / 2) / 48, y, flipped));
		}
		line[c] = 0;
		vgpu3d_log(g, "GPU3D |%s|", line);
	}

	/* one round trip: guest -> render thread -> guest */
	t0 = now_ns(g);
	for (int i = 0; i < 100; i++) {
		rc_get_renderer_version(g);
	}
	t->rtt_ns = (now_ns(g) - t0) / 100;

	/* frames: everything above, then a flush the guest waits for */
	t0 = now_ns(g);
	for (int i = 0; i < 60; i++) {
		draw_frame(g, w, h, prog, bg_blue[i & 3]);
		rc_flush_window_color_buffer(g, surf);
	}
	t->frame_ns = (now_ns(g) - t0) / 60;
	/* leave the checked picture in the colour buffer for whoever shows it */
	draw_frame(g, w, h, prog, VGPU3D_F_0_2);
	rc_flush_window_color_buffer(g, surf);

	vgpu3d_log(g, "GPU3D round_trip_us=%lu frame_us=%lu (%lu fps at %ux%u) readback_ms=%lu (%lu MB/s)",
	    (unsigned long)(t->rtt_ns / 1000), (unsigned long)(t->frame_ns / 1000),
	    (unsigned long)(t->frame_ns ? 1000000000ull / t->frame_ns : 0), w, h,
	    (unsigned long)(t->readback_ns / 1000000),
	    (unsigned long)(t->readback_ns ? (uint64_t)w * h * 4 * 1000 / t->readback_ns : 0));
	vgpu3d_log(g, "GPU3D stream bytes_out=%lu bytes_in=%lu transfers=%lu",
	    (unsigned long)g->bytes_out, (unsigned long)g->bytes_in, (unsigned long)g->transfers);
	vgpu3d_log(g, "GPU3D checks %u/%u %s", t->checks_passed, t->checks_total,
	    t->checks_passed == t->checks_total ? "PASS" : "FAIL");
	return g->err;
}
