/*
 * A compositing workload in the shape of CoreAnimation's: LAYERS full-screen textured quads,
 * blended source-over, per frame. It runs twice: on the host GPU through gfxstream (encoded
 * here, drawn by the PowerVR, the guest waiting for each frame's flush) and on the guest CPU
 * (cpublend.c, NEON, natively under KVM). The ratio is an upper bound on what moving
 * CoreAnimation's compositing off the CPU buys, for this kind of frame.
 */
#include <stdint.h>
#include <stddef.h>
#include "../../shim/uart.h"
#include "../../../xnu/files/bsd/sys/redwood_vgpu3d.h"

void cpu_blend_layer(uint8_t *dst, uint32_t w, uint32_t h, const uint8_t *tex, uint32_t tw, uint32_t th,
		     uint8_t *row, const uint16_t *xmap);
uint64_t bench_now_ns(void);
void *bench_alloc(size_t size);

#define GL_TEXTURE_2D           0x0DE1
#define GL_TEXTURE_MIN_FILTER   0x2801
#define GL_TEXTURE_MAG_FILTER   0x2800
#define GL_NEAREST              0x2600
#define GL_LINEAR               0x2601
#define GL_BLEND                0x0BE2
#define GL_SRC_ALPHA            0x0302
#define GL_ONE_MINUS_SRC_ALPHA  0x0303
#define GL_TEXTURE0             0x84C0
#define GL_FLOAT                0x1406
#define GL_TRIANGLES            0x0004
#define GL_COLOR_BUFFER_BIT     0x4000
#define GL_FRAGMENT_SHADER      0x8B30
#define GL_VERTEX_SHADER        0x8B31
#define GL_COMPILE_STATUS       0x8B81
#define GL_LINK_STATUS          0x8B82

#define TEX     256

static const char vs[] =
	"attribute vec2 pos;\nattribute vec2 uv;\nvarying vec2 t;\n"
	"void main() { t = uv; gl_Position = vec4(pos, 0.0, 1.0); }\n";
static const char fs[] =
	"precision mediump float;\nvarying vec2 t;\nuniform sampler2D s;\n"
	"void main() { gl_FragColor = texture2D(s, t); }\n";

static const uint32_t quad_pos[12] = {
	VGPU3D_F_M1, VGPU3D_F_M1, VGPU3D_F_1, VGPU3D_F_M1, VGPU3D_F_1, VGPU3D_F_1,
	VGPU3D_F_M1, VGPU3D_F_M1, VGPU3D_F_1, VGPU3D_F_1, VGPU3D_F_M1, VGPU3D_F_1,
};
static const uint32_t quad_uv[12] = {
	VGPU3D_F_0, VGPU3D_F_0, VGPU3D_F_1, VGPU3D_F_0, VGPU3D_F_1, VGPU3D_F_1,
	VGPU3D_F_0, VGPU3D_F_0, VGPU3D_F_1, VGPU3D_F_1, VGPU3D_F_0, VGPU3D_F_1,
};

static void kv(const char *k, uint64_t v) { uart_puts(k); uart_dec(v); }

void bench_composite(struct vgpu3d *g, const struct vgpu3d_selftest *t, uint32_t layers)
{
	uint32_t w = t->width, h = t->height;
	uint8_t *tex = bench_alloc(TEX * TEX * 4);
	uint8_t *frame = bench_alloc((size_t)w * h * 4);
	uint8_t *row = bench_alloc((size_t)w * 4);
	uint16_t *xmap = bench_alloc((size_t)w * 2);
	if (!tex || !frame || !row || !xmap) {
		uart_puts("GPU3D bench: no memory\n");
		return;
	}
	/* a translucent pattern: a gradient with a checker in alpha */
	for (uint32_t y = 0; y < TEX; y++)
		for (uint32_t x = 0; x < TEX; x++) {
			uint8_t *p = tex + (y * TEX + x) * 4;
			p[0] = (uint8_t)x;
			p[1] = (uint8_t)y;
			p[2] = (uint8_t)(255 - x);
			p[3] = ((x >> 5) ^ (y >> 5)) & 1 ? 96 : 32;
		}
	for (uint32_t x = 0; x < w; x++)
		xmap[x] = (uint16_t)(x * TEX / w);

	/* GPU: program, texture, state */
	uint32_t v = gl_create_shader(g, GL_VERTEX_SHADER), f = gl_create_shader(g, GL_FRAGMENT_SHADER);
	gl_shader_string(g, v, vs);
	gl_compile_shader(g, v);
	gl_shader_string(g, f, fs);
	gl_compile_shader(g, f);
	uint32_t prog = gl_create_program(g);
	gl_attach_shader(g, prog, v);
	gl_attach_shader(g, prog, f);
	gl_bind_attrib_location(g, prog, 0, "pos");
	gl_bind_attrib_location(g, prog, 1, "uv");
	gl_link_program(g, prog);
	int32_t linked = gl_get_programiv(g, prog, GL_LINK_STATUS);
	uint32_t texid = 0;
	gl_gen_textures(g, 1, &texid);
	gl_active_texture(g, GL_TEXTURE0);
	gl_bind_texture(g, GL_TEXTURE_2D, texid);
	gl_tex_parameteri(g, GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
	gl_tex_parameteri(g, GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
	uint64_t t0 = bench_now_ns();
	gl_tex_image_2d_rgba(g, GL_TEXTURE_2D, 0, TEX, TEX, tex);
	vgpu3d_flush(g);
	uint64_t upload = bench_now_ns() - t0;

	const int gpu_frames = 30;
	t0 = bench_now_ns();
	for (int fr = 0; fr < gpu_frames; fr++) {
		gl_viewport(g, 0, 0, (int32_t)w, (int32_t)h);
		gl_clear_color(g, VGPU3D_F_0, VGPU3D_F_0, VGPU3D_F_0, VGPU3D_F_1);
		gl_clear(g, GL_COLOR_BUFFER_BIT);
		gl_enable(g, GL_BLEND);
		gl_blend_func(g, GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
		gl_use_program(g, prog);
		for (uint32_t l = 0; l < layers; l++) {
			gl_vertex_attrib_pointer_data(g, 0, 2, GL_FLOAT, false, 0, quad_pos, sizeof(quad_pos));
			gl_enable_vertex_attrib_array(g, 0);
			gl_vertex_attrib_pointer_data(g, 1, 2, GL_FLOAT, false, 0, quad_uv, sizeof(quad_uv));
			gl_enable_vertex_attrib_array(g, 1);
			gl_draw_arrays(g, GL_TRIANGLES, 0, 6);
		}
		gl_disable(g, GL_BLEND);
		rc_flush_window_color_buffer(g, t->surface);
	}
	uint64_t gpu_ns = (bench_now_ns() - t0) / gpu_frames;
	rc_read_color_buffer(g, t->color_buffer, 0, 0, w, h, t->pixels);

	/* CPU: the same frame */
	const int cpu_frames = 4;
	t0 = bench_now_ns();
	for (int fr = 0; fr < cpu_frames; fr++) {
		for (size_t i = 0; i < (size_t)w * h * 4; i += 4) {
			frame[i] = frame[i + 1] = frame[i + 2] = 0;
			frame[i + 3] = 255;
		}
		for (uint32_t l = 0; l < layers; l++)
			cpu_blend_layer(frame, w, h, tex, TEX, TEX, row, xmap);
	}
	uint64_t cpu_ns = (bench_now_ns() - t0) / cpu_frames;

	/* the two pictures against each other: CPU row y is texture v = y / h, the GPU's is the
	   other way up when its readback is top-down */
	uint64_t diff = 0, big = 0;
	for (uint32_t y = 0; y < h; y++) {
		const uint8_t *gp = t->pixels + (size_t)y * w * 4;
		const uint8_t *cp = frame + (size_t)(t->rows_top_down ? h - 1 - y : y) * w * 4;
		for (uint32_t i = 0; i < w * 4; i++) {
			if ((i & 3) == 3)
				continue;
			int d = gp[i] - cp[i];
			d = d < 0 ? -d : d;
			diff += (uint64_t)d;
			big += d > 8;
		}
	}
	kv("GPU3D composite layers=", layers);
	kv(" size=", w);
	kv("x", h);
	kv(" linked=", (uint64_t)linked);
	kv(" tex_upload_us=", upload / 1000);
	kv(" gpu_frame_us=", gpu_ns / 1000);
	kv(" cpu_frame_us=", cpu_ns / 1000);
	kv(" speedup_x10=", gpu_ns ? cpu_ns * 10 / gpu_ns : 0);
	kv("\nGPU3D composite vs_cpu mean_abs_diff_x1000=", diff * 1000 / ((uint64_t)w * h * 3));
	kv(" channels_off_by_more_than_8=", big);
	kv(" gl_error=", gl_get_error(g));
	uart_puts(g->err ? " stream_error\n" : "\n");
}
