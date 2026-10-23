/*
 * virtio-gpu 3D for the window server: the guest half of the paravirtual GPU path
 * (scion docs/GPU.md, path B). The host half is crosvm's virtio-gpu with the gfxstream
 * renderer, which is what the Android Virtualization Framework ships on the Pixel 10. It
 * draws with the phone's own GPU driver (the PowerVR's OpenGL ES through EGL, or Vulkan).
 *
 * This layer covers:
 *   - the virtio-gpu 3D commands (capsets, CTX_CREATE, RESOURCE_CREATE_3D, ATTACH_BACKING,
 *     CTX_ATTACH_RESOURCE, TRANSFER_{TO,FROM}_HOST_3D, SET_SCANOUT, RESOURCE_FLUSH);
 *   - gfxstream's "pipe" transport: a PIPE_BUFFER resource with bind VIRGL_BIND_CUSTOM is a
 *     byte stream to a render thread on the host. The first write names the service
 *     ("pipe:opengles"), and replies come back through TRANSFER_FROM_HOST_3D;
 *   - the wire encoding of the renderControl (rc*, EGL-ish) and OpenGL ES 2 calls this needs.
 *     These are the encodings of gfxstream's generated encoders (guest/renderControl_enc,
 *     guest/GLESv2_enc): [opcode u32][packet size u32][arguments]. A buffer the host fills
 *     is sent as its size only, and replies are the out-buffers, then the return value.
 *
 * It sits on one function, ops->ctrl, which submits one control-queue command and waits for
 * the reply. The device and its queues belong to the virtio-gpu driver (2D display, agent D's
 * redwood_virtio_gpu), and this file never touches them. The bare-metal AVF test guest
 * (avf/gpu3d/probe) provides its own ctrl. Plain C with no allocation and no floating point
 * (GL floats are passed as IEEE-754 bit patterns, VGPU3D_F_*), so it builds into the kernel
 * and into the freestanding probe alike.
 *
 * Resource IDs 0x10000 and up belong to this layer; the 2D driver keeps those below.
 */
#ifndef _SYS_REDWOOD_VGPU3D_H_
#define _SYS_REDWOOD_VGPU3D_H_

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- virtio-gpu (virtio 1.2, section 5.7) ---------------------------------------------- */

#define VIRTIO_GPU_F_VIRGL              0
#define VIRTIO_GPU_F_EDID               1
#define VIRTIO_GPU_F_RESOURCE_UUID      2
#define VIRTIO_GPU_F_RESOURCE_BLOB      3
#define VIRTIO_GPU_F_CONTEXT_INIT       4

enum {
	VIRTIO_GPU_CMD_GET_DISPLAY_INFO = 0x0100,
	VIRTIO_GPU_CMD_RESOURCE_CREATE_2D,
	VIRTIO_GPU_CMD_RESOURCE_UNREF,
	VIRTIO_GPU_CMD_SET_SCANOUT,
	VIRTIO_GPU_CMD_RESOURCE_FLUSH,
	VIRTIO_GPU_CMD_TRANSFER_TO_HOST_2D,
	VIRTIO_GPU_CMD_RESOURCE_ATTACH_BACKING,
	VIRTIO_GPU_CMD_RESOURCE_DETACH_BACKING,
	VIRTIO_GPU_CMD_GET_CAPSET_INFO,
	VIRTIO_GPU_CMD_GET_CAPSET,
	VIRTIO_GPU_CMD_GET_EDID,
	VIRTIO_GPU_CMD_RESOURCE_ASSIGN_UUID,
	VIRTIO_GPU_CMD_RESOURCE_CREATE_BLOB,
	VIRTIO_GPU_CMD_SET_SCANOUT_BLOB,

	VIRTIO_GPU_CMD_CTX_CREATE = 0x0200,
	VIRTIO_GPU_CMD_CTX_DESTROY,
	VIRTIO_GPU_CMD_CTX_ATTACH_RESOURCE,
	VIRTIO_GPU_CMD_CTX_DETACH_RESOURCE,
	VIRTIO_GPU_CMD_RESOURCE_CREATE_3D,
	VIRTIO_GPU_CMD_TRANSFER_TO_HOST_3D,
	VIRTIO_GPU_CMD_TRANSFER_FROM_HOST_3D,
	VIRTIO_GPU_CMD_SUBMIT_3D,
	VIRTIO_GPU_CMD_RESOURCE_MAP_BLOB,
	VIRTIO_GPU_CMD_RESOURCE_UNMAP_BLOB,

	VIRTIO_GPU_RESP_OK_NODATA = 0x1100,
	VIRTIO_GPU_RESP_OK_DISPLAY_INFO,
	VIRTIO_GPU_RESP_OK_CAPSET_INFO,
	VIRTIO_GPU_RESP_OK_CAPSET,
	VIRTIO_GPU_RESP_OK_EDID,
	VIRTIO_GPU_RESP_OK_RESOURCE_UUID,
	VIRTIO_GPU_RESP_OK_MAP_INFO,

	VIRTIO_GPU_RESP_ERR_UNSPEC = 0x1200,
};

#define VIRTIO_GPU_FLAG_FENCE           (1u << 0)
#define VIRTIO_GPU_FLAG_INFO_RING_IDX   (1u << 1)

struct virtio_gpu_ctrl_hdr {
	uint32_t type;
	uint32_t flags;
	uint64_t fence_id;
	uint32_t ctx_id;
	uint8_t  ring_idx;
	uint8_t  padding[3];
};

struct virtio_gpu_config {
	uint32_t events_read;
	uint32_t events_clear;
	uint32_t num_scanouts;
	uint32_t num_capsets;
};

struct virtio_gpu_rect { uint32_t x, y, width, height; };
struct virtio_gpu_box { uint32_t x, y, z, w, h, d; };

#define VIRTIO_GPU_MAX_SCANOUTS 16
struct virtio_gpu_resp_display_info {
	struct virtio_gpu_ctrl_hdr hdr;
	struct { struct virtio_gpu_rect r; uint32_t enabled, flags; } pmodes[VIRTIO_GPU_MAX_SCANOUTS];
};

struct virtio_gpu_get_capset_info { struct virtio_gpu_ctrl_hdr hdr; uint32_t capset_index, padding; };
struct virtio_gpu_resp_capset_info {
	struct virtio_gpu_ctrl_hdr hdr;
	uint32_t capset_id, capset_max_version, capset_max_size, padding;
};
struct virtio_gpu_get_capset { struct virtio_gpu_ctrl_hdr hdr; uint32_t capset_id, capset_version; };

/* context_init: low 8 bits are the capset ID (VIRTIO_GPU_F_CONTEXT_INIT) */
struct virtio_gpu_ctx_create {
	struct virtio_gpu_ctrl_hdr hdr;
	uint32_t nlen, context_init;
	char debug_name[64];
};
struct virtio_gpu_ctx_resource { struct virtio_gpu_ctrl_hdr hdr; uint32_t resource_id, padding; };

struct virtio_gpu_resource_create_3d {
	struct virtio_gpu_ctrl_hdr hdr;
	uint32_t resource_id, target, format, bind, width, height, depth;
	uint32_t array_size, last_level, nr_samples, flags, padding;
};
struct virtio_gpu_mem_entry { uint64_t addr; uint32_t length, padding; };
struct virtio_gpu_resource_attach_backing {
	struct virtio_gpu_ctrl_hdr hdr;
	uint32_t resource_id, nr_entries;
	struct virtio_gpu_mem_entry entry[1];   /* one physically contiguous range here */
};
struct virtio_gpu_resource_unref { struct virtio_gpu_ctrl_hdr hdr; uint32_t resource_id, padding; };
struct virtio_gpu_transfer_host_3d {
	struct virtio_gpu_ctrl_hdr hdr;
	struct virtio_gpu_box box;
	uint64_t offset;
	uint32_t resource_id, level, stride, layer_stride;
};
struct virtio_gpu_set_scanout {
	struct virtio_gpu_ctrl_hdr hdr;
	struct virtio_gpu_rect r;
	uint32_t scanout_id, resource_id;
};
struct virtio_gpu_resource_flush {
	struct virtio_gpu_ctrl_hdr hdr;
	struct virtio_gpu_rect r;
	uint32_t resource_id, padding;
};

/* capset IDs (rutabaga_gfx_ffi.h) */
#define VIRTIO_GPU_CAPSET_VIRGL                 1
#define VIRTIO_GPU_CAPSET_VIRGL2                2
#define VIRTIO_GPU_CAPSET_GFXSTREAM_VULKAN      3
#define VIRTIO_GPU_CAPSET_VENUS                 4
#define VIRTIO_GPU_CAPSET_CROSS_DOMAIN          5
#define VIRTIO_GPU_CAPSET_DRM                   6
#define VIRTIO_GPU_CAPSET_GFXSTREAM_MAGMA       7
#define VIRTIO_GPU_CAPSET_GFXSTREAM_GLES        8
#define VIRTIO_GPU_CAPSET_GFXSTREAM_COMPOSER    9

/* gallium targets, virgl formats and binds as gfxstream reads them */
#define VGPU3D_PIPE_BUFFER              0
#define VGPU3D_PIPE_TEXTURE_2D          2
#define VIRGL_FORMAT_B8G8R8A8_UNORM     1
#define VIRGL_FORMAT_R8_UNORM           64
#define VIRGL_FORMAT_R8G8B8A8_UNORM     67
#define VIRGL_BIND_RENDER_TARGET        (1u << 1)
#define VIRGL_BIND_SAMPLER_VIEW         (1u << 3)
#define VIRGL_BIND_CUSTOM               (1u << 17)
#define VIRGL_BIND_SCANOUT              (1u << 18)

/* ---- what the layer needs from its host ------------------------------------------------ */

struct vgpu3d_ops {
	void *arg;
	/*
	 * Submit cmd on the control queue and wait for the device's reply in resp. 0 when the
	 * device answered (the reply type is checked here), an errno when the transport failed or
	 * timed out. Must serialize with every other user of the control queue.
	 */
	int (*ctrl)(void *arg, const void *cmd, uint32_t cmd_len, void *resp, uint32_t resp_len);
	/* Zeroed, physically contiguous, CPU-visible memory the device can read and write. */
	void *(*dma_alloc)(void *arg, size_t size, uint64_t *pa);
	/* One line of log, without the newline. */
	void (*log)(void *arg, const char *line);
	/* A monotonic clock, for the self-test's timings. */
	uint64_t (*now_ns)(void *arg);
};

#define VGPU3D_MAX_CAPSETS      16
#define VGPU3D_FIRST_RESOURCE   0x10000u

struct vgpu3d {
	struct vgpu3d_ops ops;
	uint64_t features;                      /* negotiated virtio-gpu features */
	uint32_t num_capsets;
	struct { uint32_t id, max_version, max_size; } capset[VGPU3D_MAX_CAPSETS];
	uint32_t gles_capset[4];                /* gfxstream glesCapset: version, ring, buffer, align */

	uint32_t ctx_id;                        /* virtio-gpu context of the pipe, 0 = none */
	uint32_t ctx_capset;
	uint32_t next_resource;
	uint32_t pipe_res;                      /* the PIPE_BUFFER resource */
	uint8_t *pipe_va;                       /* its backing: the staging buffer of both directions */
	uint64_t pipe_pa;
	uint32_t pipe_size;
	uint32_t wpos;                          /* bytes encoded and not yet sent */
	int err;                                /* first error; every later call is a no-op */
	uint64_t bytes_out, bytes_in, transfers;
};

/* ---- the layer ------------------------------------------------------------------------- */

/* printf-like line through ops->log (%s %c %d %u %x %p with l/ll and a 0-padded width) */
void vgpu3d_log(struct vgpu3d *g, const char *fmt, ...) __attribute__((format(printf, 2, 3)));

/*
 * Lists the capsets (GET_CAPSET_INFO 0..num_capsets-1; VGPU3D_PROBE_CAPSETS: until the device
 * refuses one) and reads gfxstream-gles's. 0 or errno.
 */
#define VGPU3D_PROBE_CAPSETS    0xffffffffu
int vgpu3d_query(struct vgpu3d *g, uint64_t features, uint32_t num_capsets);
/* The capset is offered (by ID). */
bool vgpu3d_has_capset(const struct vgpu3d *g, uint32_t id);
/*
 * Creates a context of this capset (0 = the host's default) and the pipe, and connects it
 * to "pipe:opengles": a render thread on the host, decoding renderControl and GLES. 0 or errno.
 */
int vgpu3d_open(struct vgpu3d *g, uint32_t capset, uint32_t pipe_size);
/* A 2D RGBA host colour buffer as a resource (RESOURCE_CREATE_3D), for scanout; 0 on failure. */
uint32_t vgpu3d_create_scanout_resource(struct vgpu3d *g, uint32_t width, uint32_t height);
/* SET_SCANOUT + RESOURCE_FLUSH of a whole resource. */
int vgpu3d_present(struct vgpu3d *g, uint32_t scanout, uint32_t res, uint32_t width, uint32_t height);

/*
 * A 2D resource of this context (RESOURCE_CREATE_3D + ATTACH_BACKING + CTX_ATTACH_RESOURCE) with
 * the given backing pages; gfxstream makes it a colour buffer whose handle is `res`. scratch
 * holds the ATTACH_BACKING command. 0 or errno.
 */
int vgpu3d_resource_create(struct vgpu3d *g, uint32_t res, uint32_t width, uint32_t height, uint32_t format,
    uint32_t bind, const struct virtio_gpu_mem_entry *entries, uint32_t nentries, void *scratch, uint32_t scratch_size);
int vgpu3d_resource_unref(struct vgpu3d *g, uint32_t res);
/* TRANSFER_{TO,FROM}_HOST_3D of a box; the backing is width * 4 bytes a row (gfxstream's layout). */
int vgpu3d_transfer_box(struct vgpu3d *g, uint32_t res, bool to_host, uint32_t x, uint32_t y, uint32_t w,
    uint32_t h, uint64_t offset);
/* The pipe and the context away. */
void vgpu3d_close(struct vgpu3d *g);

/* The stream: space for n bytes of encoded calls, sending what is queued when it is full. */
uint8_t *vgpu3d_alloc(struct vgpu3d *g, uint32_t n);
/* Send what is queued (TRANSFER_TO_HOST_3D). */
int vgpu3d_flush(struct vgpu3d *g);
/* Send, then read exactly n bytes of replies (TRANSFER_FROM_HOST_3D, in pipe-sized pieces). */
int vgpu3d_read(struct vgpu3d *g, void *buf, uint32_t n);

/* ---- renderControl and GLES 2, as gfxstream encodes them (return 0 / the host's value) -- */

uint32_t rc_get_renderer_version(struct vgpu3d *g);
int32_t  rc_get_gl_string(struct vgpu3d *g, uint32_t name, char *buf, uint32_t size);
int32_t  rc_choose_config(struct vgpu3d *g, const int32_t *attribs, uint32_t attribs_bytes,
			  uint32_t *configs, uint32_t nconfigs);
uint32_t rc_create_context(struct vgpu3d *g, uint32_t config, uint32_t share, uint32_t gles_api);
uint32_t rc_create_window_surface(struct vgpu3d *g, uint32_t config, uint32_t w, uint32_t h);
uint32_t rc_create_color_buffer(struct vgpu3d *g, uint32_t w, uint32_t h, uint32_t internal_format);
void     rc_set_window_color_buffer(struct vgpu3d *g, uint32_t surface, uint32_t color_buffer);
int32_t  rc_make_current(struct vgpu3d *g, uint32_t ctx, uint32_t draw, uint32_t read);
int32_t  rc_flush_window_color_buffer(struct vgpu3d *g, uint32_t surface);
int      rc_read_color_buffer(struct vgpu3d *g, uint32_t cb, uint32_t x, uint32_t y, uint32_t w,
			      uint32_t h, void *pixels);   /* GL_RGBA, GL_UNSIGNED_BYTE */

void gl_viewport(struct vgpu3d *g, int32_t x, int32_t y, int32_t w, int32_t h);
void gl_clear_color(struct vgpu3d *g, uint32_t r, uint32_t gr, uint32_t b, uint32_t a); /* VGPU3D_F_* */
void gl_clear(struct vgpu3d *g, uint32_t mask);
void gl_enable(struct vgpu3d *g, uint32_t cap);
void gl_disable(struct vgpu3d *g, uint32_t cap);
void gl_scissor(struct vgpu3d *g, int32_t x, int32_t y, int32_t w, int32_t h);
void gl_finish(struct vgpu3d *g);
uint32_t gl_get_error(struct vgpu3d *g);
uint32_t gl_create_shader(struct vgpu3d *g, uint32_t type);
void gl_shader_string(struct vgpu3d *g, uint32_t shader, const char *src);
void gl_compile_shader(struct vgpu3d *g, uint32_t shader);
int32_t gl_get_shaderiv(struct vgpu3d *g, uint32_t shader, uint32_t pname);
uint32_t gl_create_program(struct vgpu3d *g);
void gl_attach_shader(struct vgpu3d *g, uint32_t program, uint32_t shader);
void gl_bind_attrib_location(struct vgpu3d *g, uint32_t program, uint32_t index, const char *name);
void gl_link_program(struct vgpu3d *g, uint32_t program);
int32_t gl_get_programiv(struct vgpu3d *g, uint32_t program, uint32_t pname);
void gl_use_program(struct vgpu3d *g, uint32_t program);
void gl_vertex_attrib_pointer_data(struct vgpu3d *g, uint32_t index, int32_t size, uint32_t type,
				   bool normalized, int32_t stride, const void *data, uint32_t len);
void gl_enable_vertex_attrib_array(struct vgpu3d *g, uint32_t index);
void gl_draw_arrays(struct vgpu3d *g, uint32_t mode, int32_t first, int32_t count);
void gl_gen_textures(struct vgpu3d *g, uint32_t n, uint32_t *textures);
void gl_bind_texture(struct vgpu3d *g, uint32_t target, uint32_t texture);
void gl_tex_parameteri(struct vgpu3d *g, uint32_t target, uint32_t pname, int32_t param);
/* GL_RGBA / GL_UNSIGNED_BYTE, rows packed (width * 4 bytes, the default unpack alignment) */
void gl_tex_image_2d_rgba(struct vgpu3d *g, uint32_t target, int32_t level, int32_t w, int32_t h,
			  const void *pixels);
void gl_blend_func(struct vgpu3d *g, uint32_t sfactor, uint32_t dfactor);
void gl_active_texture(struct vgpu3d *g, uint32_t unit);

/* ---- the self-test: bring-up, a picture drawn by the host GPU, timings ----------------- */

struct vgpu3d_selftest {
	uint32_t width, height;                 /* in: the picture's size */
	uint8_t *pixels;                        /* in: width * height * 4 bytes, for the readback */
	uint32_t scanout_res;                   /* in: draw into this resource as well (0 = none) */
	/* out */
	uint32_t context, surface, color_buffer;        /* left current, for more drawing */
	char vendor[96], renderer[160], version[96];
	uint32_t renderer_version;
	uint32_t checks_passed, checks_total;
	uint32_t fnv;                           /* FNV-1a of the readback */
	bool rows_top_down;                     /* readback row 0 is the top of the picture */
	uint64_t rtt_ns;                        /* one round trip (rcGetRendererVersion) */
	uint64_t frame_ns;                      /* clear + 3 scissor clears + a triangle + flush */
	uint64_t readback_ns;                   /* the whole picture back into the guest */
};
int vgpu3d_selftest(struct vgpu3d *g, struct vgpu3d_selftest *t);

/* IEEE-754 single bit patterns for GL calls */
#define VGPU3D_F_0      0x00000000u
#define VGPU3D_F_0_1    0x3dcccccdu
#define VGPU3D_F_0_2    0x3e4ccccdu
#define VGPU3D_F_0_25   0x3e800000u
#define VGPU3D_F_0_5    0x3f000000u
#define VGPU3D_F_0_75   0x3f400000u
#define VGPU3D_F_0_8    0x3f4ccccdu
#define VGPU3D_F_1      0x3f800000u
#define VGPU3D_F_M0_5   0xbf000000u
#define VGPU3D_F_M0_75  0xbf400000u
#define VGPU3D_F_M1     0xbf800000u

#ifdef __cplusplus
}
#endif

#endif /* _SYS_REDWOOD_VGPU3D_H_ */
