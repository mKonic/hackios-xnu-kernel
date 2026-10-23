// HackiOS emulator: boots the system under QEMU (qemu/run-ios.sh) and shows it in an iPhone X frame
// with a toolbar beside it, like Android's emulator. The screen is the guest's ramfb (QEMU's D-Bus
// display), the left mouse button is a finger on the guest's virtio tablet, and the toolbar's
// buttons press the phone's buttons as keys on its virtio keyboard.
//
//   emu [--secs N] [--name NAME] [-- extra qemu args...]
//
// The host keyboard reaches the guest too: H home, L lock, Up/Down volume, F11 five fingers, and
// F12 NAME Enter for the live stacks of a process (see the kernel's redwood_virtio_input.c).
// Each instance (--name, default "emu") keeps its QMP socket and QEMU's serial log in
// out/emu/run/<name>/; screenshots (the camera button, or SIGUSR1) go to out/emu/screenshots/.

#include "png.h"
#include "qemu_link.h"

#define STB_IMAGE_IMPLEMENTATION
#include "third_party/stb_image.h"

#include <SDL3/SDL.h>
#include <SDL3/SDL_opengles2.h>

#include <algorithm>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <ctime>
#include <filesystem>
#include <string>
#include <vector>

#include <fcntl.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef HACKIOS_ROOT
#error HACKIOS_ROOT must name the source tree
#endif
#ifndef EMU_ICONS
#error EMU_ICONS must name the rasterised icon directory
#endif

namespace {

volatile std::sig_atomic_t screenshot_requested = 0;

// Device geometry in points: the iPhone X (iPhone10,3, the IPSW HackiOS runs) is 375 x 812 pt at
// @3x, the 1125 x 2436 panel MiniIOMobileFramebuffer drives. Its display corners and the notch.
constexpr float kScreenW = 375, kScreenH = 812, kScreenR = 39;
constexpr float kBezel = 13, kBand = 3.5f;
constexpr float kNotchW = 209, kNotchH = 30, kNotchR = 20;
constexpr float kPhoneW = kScreenW + 2 * kBezel, kPhoneH = kScreenH + 2 * kBezel;
constexpr float kGap = 12, kBarW = 46, kButton = 46;
constexpr float kWindowW = kPhoneW + kGap + kBarW, kWindowH = kPhoneH;

struct Button {
	const char* icon;
	uint32_t qnum;  // QEMU qnum key code; 0 = handled here
	const char* tip;
};
// Window controls first, then the phone's buttons, then screenshot.
const Button kButtons[] = {
	{"minus", 0, "minimise"},       {"x", 0, "close"},
	{"power", 0xde, "power"},       {"speaker-high", 0xb0, "volume up"}, {"speaker-low", 0xae, "volume down"},
	{"house", 0xc7, "home"},        {"camera", 0, "screenshot"},
};
constexpr int kButtons_n = sizeof kButtons / sizeof kButtons[0];

// The host keys the guest's keyboard driver acts on, as QEMU qnum codes.
uint32_t qnum_for(SDL_Scancode sc) {
	static const uint8_t letters[26] = {0x1e, 0x30, 0x2e, 0x20, 0x12, 0x21, 0x22, 0x23, 0x17, 0x24, 0x25, 0x26, 0x32,
	                                    0x31, 0x18, 0x19, 0x10, 0x13, 0x1f, 0x14, 0x16, 0x2f, 0x11, 0x2d, 0x15, 0x2c};
	if (sc >= SDL_SCANCODE_A && sc <= SDL_SCANCODE_Z) return letters[sc - SDL_SCANCODE_A];
	if (sc >= SDL_SCANCODE_1 && sc <= SDL_SCANCODE_9) return 0x02 + (sc - SDL_SCANCODE_1);
	switch (sc) {
	case SDL_SCANCODE_0: return 0x0b;
	case SDL_SCANCODE_MINUS: return 0x0c;
	case SDL_SCANCODE_RETURN: return 0x1c;
	case SDL_SCANCODE_LSHIFT: return 0x2a;
	case SDL_SCANCODE_RSHIFT: return 0x36;
	case SDL_SCANCODE_SPACE: return 0x39;
	case SDL_SCANCODE_F11: return 0x57;
	case SDL_SCANCODE_F12: return 0x58;
	case SDL_SCANCODE_HOME: return 0xc7;
	case SDL_SCANCODE_UP: return 0xc8;
	case SDL_SCANCODE_DOWN: return 0xd0;
	default: return 0;
	}
}

// Button i's centre in window points. The two window controls sit in a short row at the top.
SDL_FPoint button_centre(int i) {
	float x0 = kPhoneW + kGap;
	if (i < 2) return {x0 + 12 + i * 22.0f, 14};
	return {x0 + kBarW / 2, 28 + 16 + (i - 2) * kButton + kButton / 2};
}
float button_radius(int i) { return i < 2 ? 9 : 17; }
float bar_height() { return button_centre(kButtons_n - 1).y + kButton / 2 + 6; }

const char* kVertex = R"(#version 300 es
in vec2 pos;
uniform vec4 rect;  // x, y, w, h in window points
uniform vec2 window;
out vec2 p;         // position in points, relative to rect
void main() {
	p = pos * rect.zw;
	vec2 w = rect.xy + p;
	gl_Position = vec4(w / window * 2.0 - 1.0, 0.0, 1.0);
	gl_Position.y = -gl_Position.y;
})";

// mode 0: the phone (band, bezel, screen with the guest's frame, notch). mode 1: toolbar panel.
// mode 2: icon (alpha mask) over an optional hover disc. mode 3: plain copy of the screen texture
// (for screenshots). Shapes are SDF rounded rects, antialiased over one pixel.
const char* kFragment = R"(#version 300 es
precision highp float;
in vec2 p;
uniform vec4 rect;
uniform int mode;
uniform float px;       // points per pixel
uniform sampler2D tex;
uniform bool have_tex;
uniform vec2 footprint;  // one window pixel's extent in the screen texture (uv); 0 = one tap
uniform float hover;
uniform vec3 screen;     // screen w, h, corner radius
uniform vec4 frame;      // bezel, band, notch w, notch h
uniform float notch_r;
out vec4 color;

float sd_round(vec2 q, vec2 half_size, float r) {
	vec2 d = abs(q) - half_size + r;
	return length(max(d, 0.0)) + min(max(d.x, d.y), 0.0) - r;
}
float cover(float d) { return clamp(0.5 - d / px, 0.0, 1.0); }
vec4 over(vec4 top, vec4 under) { return top + under * (1.0 - top.a); }

vec3 screen_pixel(vec2 uv) {
	vec3 c;
	if (footprint.x > 0.0) {
		// Shrinking the screen to the window: average a 4 x 4 grid of bilinear taps over the
		// pixel's footprint, so thin lines stay smooth instead of skipping texels.
		c = vec3(0.0);
		for (int j = 0; j < 4; j++)
			for (int i = 0; i < 4; i++)
				c += texture(tex, uv + (vec2(float(i), float(j)) - 1.5) / 4.0 * footprint).rgb;
		c /= 16.0;
	} else {
		c = texture(tex, uv).rgb;
	}
	return c.bgr;  // pixman x8r8g8b8 is B, G, R, X in memory
}

void main() {
	vec2 size = rect.zw;
	if (mode == 0) {
		float bezel = frame.x, band = frame.y;
		vec2 c = p - size * 0.5;
		float d_body = sd_round(c, size * 0.5, screen.z + bezel);
		// Stainless band: polished grey, lighter towards the top-left.
		float t = dot(p / size, vec2(0.35, 0.65));
		vec3 metal = mix(vec3(0.86, 0.86, 0.88), vec3(0.52, 0.53, 0.55), t);
		float edge = smoothstep(0.0, band, -d_body);
		metal *= 0.82 + 0.18 * (1.0 - abs(edge * 2.0 - 1.0));
		vec4 col = vec4(metal, 1.0) * cover(d_body);
		float d_bezel = d_body + band;
		col = over(vec4(vec3(0.015), 1.0) * cover(d_bezel), col * (1.0 - cover(d_bezel)));
		vec2 sc = p - vec2(bezel, bezel);
		float d_screen = sd_round(sc - screen.xy * 0.5, screen.xy * 0.5, screen.z);
		vec3 pixel = have_tex ? screen_pixel(sc / screen.xy) : vec3(0.0);
		col = mix(col, vec4(pixel, 1.0), cover(d_screen));
		// The notch hangs from the top edge: a rounded rect whose top half is off the screen, cut to it.
		float d_notch = max(sd_round(sc - vec2(screen.x * 0.5, 0.0), vec2(frame.z * 0.5, frame.w), notch_r), d_screen);
		col = mix(col, vec4(0.0, 0.0, 0.0, 1.0), cover(d_notch));
		color = col;
	} else if (mode == 1) {
		float d = sd_round(p - size * 0.5, size * 0.5, 10.0);
		vec4 border = vec4(vec3(0.70), 1.0) * cover(d);
		color = over(vec4(vec3(0.975), 1.0) * cover(d + 1.0), border * (1.0 - cover(d + 1.0)));
	} else if (mode == 2) {
		float d = length(p - size * 0.5) - size.x * 0.5;
		vec4 disc = vec4(vec3(0.0), 0.08 * hover) * cover(d);
		float a = texture(tex, p / size).a;
		color = over(vec4(vec3(0.22), 1.0) * a, vec4(disc.rgb * disc.a, disc.a));
	} else {
		color = vec4(screen_pixel(p / size), 1.0);
	}
})";

GLuint compile(GLenum type, const char* src) {
	GLuint s = glCreateShader(type);
	glShaderSource(s, 1, &src, nullptr);
	glCompileShader(s);
	GLint ok;
	glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
	if (!ok) {
		char log[2048];
		glGetShaderInfoLog(s, sizeof log, nullptr, log);
		std::fprintf(stderr, "emu: shader: %s\n", log);
		std::exit(1);
	}
	return s;
}

struct Emu {
	SDL_Window* window = nullptr;
	emu::QemuLink link;
	pid_t qemu = -1;
	std::string name = "emu";
	int secs = 3600;
	std::vector<std::string> qemu_args;
	std::string dir() const { return std::string(HACKIOS_ROOT) + "/out/emu/run/" + name; }

	GLuint prog = 0, vbo = 0, screen_tex = 0;
	GLuint icons[kButtons_n] = {};
	bool have_frame = false;
	uint64_t drawn_serial = ~0ull;

	float k = 1;  // window points → window pixels (SDL logical units)
	int hover = -1, pressed = -1;
	bool touching = false;

	bool start_qemu(std::string* error);
	void quit_qemu();
	void setup_gl();
	void load_icons();
	void update_texture();
	void draw();
	bool screenshot();
	bool on_screen(float x, float y, double* gx, double* gy) const;
	int button_at(float x, float y) const;
};

bool Emu::start_qemu(std::string* error) {
	std::filesystem::create_directories(dir());
	std::string log = dir() + "/serial.log", qmp = dir() + "/qmp.sock";
	std::filesystem::remove(qmp);
	qemu = fork();
	if (qemu < 0) return *error = "fork failed", false;
	if (qemu == 0) {
		setpgid(0, 0);
		int fd = open(log.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
		int null = open("/dev/null", O_RDONLY | O_CLOEXEC);
		dup2(null, STDIN_FILENO), dup2(fd, STDOUT_FILENO), dup2(fd, STDERR_FILENO);
		setenv("EMU_QMP", qmp.c_str(), 1);
		std::string script = std::string(HACKIOS_ROOT) + "/qemu/run-ios.sh", s = std::to_string(secs);
		std::vector<char*> argv = {script.data(), s.data()};
		for (auto& a : qemu_args) argv.push_back(a.data());
		argv.push_back(nullptr);
		execv(script.c_str(), argv.data());
		_exit(127);
	}
	setpgid(qemu, qemu);
	std::printf("emu: QEMU pid %d, serial log %s\n", qemu, log.c_str());
	std::fflush(stdout);
	// run-ios.sh first lays the system image into qemu/ram.img and packs the payload: give it time
	return link.connect(qmp, 180000, error);
}

void Emu::quit_qemu() {
	if (qemu <= 0) return;
	kill(-qemu, SIGTERM);  // our QEMU's process group only
	for (int i = 0; i < 50 && waitpid(qemu, nullptr, WNOHANG) == 0; ++i) usleep(20'000);
	if (waitpid(qemu, nullptr, WNOHANG) == 0) kill(-qemu, SIGKILL), waitpid(qemu, nullptr, 0);
	qemu = -1;
}

void Emu::setup_gl() {
	prog = glCreateProgram();
	glAttachShader(prog, compile(GL_VERTEX_SHADER, kVertex));
	glAttachShader(prog, compile(GL_FRAGMENT_SHADER, kFragment));
	glBindAttribLocation(prog, 0, "pos");
	glLinkProgram(prog);
	const float quad[] = {0, 0, 1, 0, 0, 1, 1, 1};
	glGenBuffers(1, &vbo);
	glBindBuffer(GL_ARRAY_BUFFER, vbo);
	glBufferData(GL_ARRAY_BUFFER, sizeof quad, quad, GL_STATIC_DRAW);
	glGenTextures(1, &screen_tex);
	glBindTexture(GL_TEXTURE_2D, screen_tex);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
	load_icons();
}

void Emu::load_icons() {
	for (int i = 0; i < kButtons_n; ++i) {
		std::string path = std::string(EMU_ICONS) + "/" + kButtons[i].icon + ".png";
		int w, h, n;
		unsigned char* px = stbi_load(path.c_str(), &w, &h, &n, 4);
		if (!px) {
			std::fprintf(stderr, "emu: icon %s: %s\n", path.c_str(), stbi_failure_reason());
			continue;
		}
		glGenTextures(1, &icons[i]);
		glBindTexture(GL_TEXTURE_2D, icons[i]);
		glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, px);
		glGenerateMipmap(GL_TEXTURE_2D);
		glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR_MIPMAP_LINEAR);
		glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
		stbi_image_free(px);
	}
}

// Brings the screen texture up to date with what QEMU last sent.
void Emu::update_texture() {
	emu::Scanout& s = link.scanout();
	drawn_serial = s.serial;
	have_frame = s.enabled && s.width;
	if (!have_frame) return;
	glBindTexture(GL_TEXTURE_2D, screen_tex);
	glPixelStorei(GL_UNPACK_ALIGNMENT, 4);
	glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, GLsizei(s.width), GLsizei(s.height), 0, GL_RGBA, GL_UNSIGNED_BYTE, s.pixels.data());
}

void Emu::draw() {
	int pw, ph;
	SDL_GetWindowSizeInPixels(window, &pw, &ph);
	glViewport(0, 0, pw, ph);
	glClearColor(0, 0, 0, 0);
	glClear(GL_COLOR_BUFFER_BIT);
	glEnable(GL_BLEND);
	glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA);
	glUseProgram(prog);
	glBindBuffer(GL_ARRAY_BUFFER, vbo);
	glEnableVertexAttribArray(0);
	glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, nullptr);
	glUniform2f(glGetUniformLocation(prog, "window"), kWindowW, kWindowH);
	glUniform1f(glGetUniformLocation(prog, "px"), kWindowH / float(ph));
	glUniform1i(glGetUniformLocation(prog, "tex"), 0);
	glUniform3f(glGetUniformLocation(prog, "screen"), kScreenW, kScreenH, kScreenR);
	glUniform4f(glGetUniformLocation(prog, "frame"), kBezel, kBand, kNotchW, kNotchH);
	glUniform1f(glGetUniformLocation(prog, "notch_r"), kNotchR);
	auto quad = [&](int mode, float x, float y, float w, float h) {
		glUniform1i(glGetUniformLocation(prog, "mode"), mode);
		glUniform4f(glGetUniformLocation(prog, "rect"), x, y, w, h);
		glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
	};
	glActiveTexture(GL_TEXTURE0);
	glBindTexture(GL_TEXTURE_2D, screen_tex);
	glUniform1i(glGetUniformLocation(prog, "have_tex"), have_frame);
	const float window_px_per_pt = float(ph) / kWindowH;
	glUniform2f(glGetUniformLocation(prog, "footprint"), 1 / (window_px_per_pt * kScreenW), 1 / (window_px_per_pt * kScreenH));
	quad(0, 0, 0, kPhoneW, kPhoneH);
	quad(1, kPhoneW + kGap, 0, kBarW, bar_height());
	for (int i = 0; i < kButtons_n; ++i) {
		SDL_FPoint c = button_centre(i);
		float r = button_radius(i);
		glBindTexture(GL_TEXTURE_2D, icons[i]);
		glUniform1f(glGetUniformLocation(prog, "hover"), i == pressed ? 2.0f : i == hover ? 1.0f : 0.0f);
		quad(2, c.x - r, c.y - r, 2 * r, 2 * r);
	}
	SDL_GL_SwapWindow(window);
}

// Saves the guest's frame at its own resolution, straight from the scanout QEMU sent.
bool Emu::screenshot() {
	emu::Scanout& s = link.scanout();
	if (!have_frame) return false;
	std::vector<uint8_t> rgba(s.pixels.size());
	for (size_t i = 0; i + 3 < s.pixels.size(); i += 4)
		rgba[i] = s.pixels[i + 2], rgba[i + 1] = s.pixels[i + 1], rgba[i + 2] = s.pixels[i], rgba[i + 3] = 255;
	std::string out = std::string(HACKIOS_ROOT) + "/out/emu/screenshots";
	std::filesystem::create_directories(out);
	char file[64];
	std::time_t now = std::time(nullptr);
	std::strftime(file, sizeof file, "/emu-%Y%m%d-%H%M%S.png", std::localtime(&now));
	out += file;
	bool ok = emu::write_png(out, rgba.data(), s.width, s.height);
	if (ok) std::printf("emu: screenshot %s\n", out.c_str());
	std::fflush(stdout);
	return ok;
}

bool Emu::on_screen(float x, float y, double* gx, double* gy) const {
	const emu::Scanout& s = const_cast<Emu*>(this)->link.scanout();
	float px = x / k - kBezel, py = y / k - kBezel;
	if (px < 0 || py < 0 || px > kScreenW || py > kScreenH || !s.width) return false;
	*gx = px / kScreenW * s.width, *gy = py / kScreenH * s.height;
	return true;
}

int Emu::button_at(float x, float y) const {
	for (int i = 0; i < kButtons_n; ++i) {
		SDL_FPoint c = button_centre(i);
		if (std::hypot(x / k - c.x, y / k - c.y) <= button_radius(i) + 2) return i;
	}
	return -1;
}

SDL_HitTestResult hit_test(SDL_Window*, const SDL_Point* pt, void* data) {
	auto* e = static_cast<Emu*>(data);
	float x = pt->x / e->k, y = pt->y / e->k;
	if (e->button_at(float(pt->x), float(pt->y)) >= 0) return SDL_HITTEST_NORMAL;
	bool in_screen = x >= kBezel && y >= kBezel && x <= kBezel + kScreenW && y <= kBezel + kScreenH;
	bool in_phone = x <= kPhoneW && y <= kPhoneH;
	bool in_bar = x >= kPhoneW + kGap && y <= bar_height();
	return (in_phone && !in_screen) || in_bar ? SDL_HITTEST_DRAGGABLE : SDL_HITTEST_NORMAL;
}

}  // namespace

int main(int argc, char** argv) {
	Emu e;
	for (int i = 1; i < argc; ++i) {
		std::string a = argv[i];
		if (a == "--secs" && i + 1 < argc) e.secs = std::atoi(argv[++i]);
		else if (a == "--name" && i + 1 < argc) e.name = argv[++i];
		else if (a == "--") {
			e.qemu_args.assign(argv + i + 1, argv + argc);
			break;
		} else return std::fprintf(stderr, "usage: emu [--secs N] [--name NAME] [-- extra qemu args...]\n"), 2;
	}
	std::signal(SIGPIPE, SIG_IGN);
	std::signal(SIGUSR1, [](int) { screenshot_requested = 1; });
	SDL_SetHint(SDL_HINT_APP_ID, "hackios-emu");
	if (!SDL_Init(SDL_INIT_VIDEO)) return std::fprintf(stderr, "emu: %s\n", SDL_GetError()), 1;

	// Fit the phone to ~92% of the display's height, never above 2 pixels a point.
	const SDL_DisplayMode* mode = SDL_GetDesktopDisplayMode(SDL_GetPrimaryDisplay());
	float avail = mode ? mode->h * 0.92f : 900;
	e.k = std::min(2.0f, avail / kWindowH);
	SDL_GL_SetAttribute(SDL_GL_CONTEXT_PROFILE_MASK, SDL_GL_CONTEXT_PROFILE_ES);
	SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 3);
	SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, 0);
	SDL_GL_SetAttribute(SDL_GL_ALPHA_SIZE, 8);
	e.window = SDL_CreateWindow("HackiOS", int(std::lround(kWindowW * e.k)), int(std::lround(kWindowH * e.k)),
	                            SDL_WINDOW_OPENGL | SDL_WINDOW_BORDERLESS | SDL_WINDOW_TRANSPARENT | SDL_WINDOW_HIGH_PIXEL_DENSITY);
	if (!e.window) return std::fprintf(stderr, "emu: window: %s\n", SDL_GetError()), 1;
	SDL_GLContext gl = SDL_GL_CreateContext(e.window);
	if (!gl) return std::fprintf(stderr, "emu: GL: %s\n", SDL_GetError()), 1;
	SDL_GL_SetSwapInterval(1);
	e.setup_gl();
	SDL_SetWindowHitTest(e.window, hit_test, &e);
	e.draw();  // the frame shows while QEMU starts

	std::string err;
	if (!e.start_qemu(&err)) {
		std::fprintf(stderr, "emu: %s\n", err.c_str());
		e.quit_qemu();
		return 1;
	}
	bool running = true, dirty = true;
	while (running) {
		e.link.pump();
		if (waitpid(e.qemu, nullptr, WNOHANG) == e.qemu) {
			std::fprintf(stderr, "emu: QEMU exited\n");
			e.qemu = -1;
			break;
		}
		SDL_Event ev;
		while (SDL_PollEvent(&ev)) {
			double gx = 0, gy = 0;
			switch (ev.type) {
			case SDL_EVENT_QUIT: running = false; break;
			case SDL_EVENT_WINDOW_EXPOSED:
			case SDL_EVENT_WINDOW_PIXEL_SIZE_CHANGED: dirty = true; break;
			case SDL_EVENT_KEY_DOWN:
			case SDL_EVENT_KEY_UP:
				if (ev.key.repeat) break;
				if (uint32_t q = qnum_for(ev.key.scancode)) e.link.key(q, ev.type == SDL_EVENT_KEY_DOWN);
				break;
			case SDL_EVENT_MOUSE_MOTION: {
				int h = e.button_at(ev.motion.x, ev.motion.y);
				if (h != e.hover) e.hover = h, dirty = true;
				if (e.touching && e.on_screen(std::clamp(ev.motion.x, kBezel * e.k, (kBezel + kScreenW) * e.k),
				                              std::clamp(ev.motion.y, kBezel * e.k, (kBezel + kScreenH) * e.k), &gx, &gy))
					e.link.finger_move(gx, gy);
				break;
			}
			case SDL_EVENT_WINDOW_MOUSE_LEAVE:
				if (e.hover >= 0) e.hover = -1, dirty = true;
				break;
			case SDL_EVENT_MOUSE_BUTTON_DOWN:
				if (ev.button.button != SDL_BUTTON_LEFT) break;
				if (int b = e.button_at(ev.button.x, ev.button.y); b >= 0) {
					e.pressed = b, dirty = true;
					if (kButtons[b].qnum) e.link.key(kButtons[b].qnum, true);
				} else if (e.on_screen(ev.button.x, ev.button.y, &gx, &gy)) {
					e.touching = true;
					SDL_CaptureMouse(true);
					e.link.finger_move(gx, gy);
					e.link.finger(true);
				}
				break;
			case SDL_EVENT_MOUSE_BUTTON_UP:
				if (ev.button.button != SDL_BUTTON_LEFT) break;
				if (e.pressed >= 0) {
					int b = e.pressed;
					e.pressed = -1, dirty = true;
					if (kButtons[b].qnum) e.link.key(kButtons[b].qnum, false);
					else if (e.button_at(ev.button.x, ev.button.y) == b) {
						std::string icon = kButtons[b].icon;
						if (icon == "x") running = false;
						else if (icon == "minus") SDL_MinimizeWindow(e.window);
						else if (icon == "camera") e.screenshot();
					}
				}
				if (e.touching) {
					e.touching = false;
					SDL_CaptureMouse(false);
					e.on_screen(std::clamp(ev.button.x, kBezel * e.k, (kBezel + kScreenW) * e.k),
					            std::clamp(ev.button.y, kBezel * e.k, (kBezel + kScreenH) * e.k), &gx, &gy);
					e.link.finger_move(gx, gy);
					e.link.finger(false);
				}
				break;
			}
		}
		if (e.link.scanout().serial != e.drawn_serial) e.update_texture(), dirty = true;
		if (screenshot_requested) screenshot_requested = 0, e.screenshot();
		bool drew = dirty;
		if (dirty) e.draw(), dirty = false;
		if (!drew) SDL_WaitEventTimeout(nullptr, 4);
	}
	e.quit_qemu();
	SDL_GL_DestroyContext(gl);
	SDL_DestroyWindow(e.window);
	SDL_Quit();
	return 0;
}
