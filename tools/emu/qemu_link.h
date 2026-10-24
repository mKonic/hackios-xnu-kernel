// The emulator's line to QEMU: QMP to attach as a D-Bus display client (`-display dbus,p2p=yes`),
// then org.qemu.Display1 over that peer connection. QEMU calls our Listener with the guest's
// scanout (ramfb: pixel data); we send the finger back as the absolute pointer of the guest's
// virtio tablet and the phone's buttons as keys on its virtio keyboard.
#pragma once

#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

typedef struct _GDBusConnection GDBusConnection;
typedef struct _GMainContext GMainContext;

namespace emu {

struct Scanout {
	// Tightly packed BGRA (pixman x8r8g8b8 / a8r8g8b8), width * height * 4.
	std::vector<uint8_t> pixels;
	uint32_t width = 0, height = 0;
	uint64_t serial = 0;  // bumps on every Scanout/Update: redraw when it changes
	// The picture QEMU shares as memory (Listener.Unix.Map: ScanoutMap, then UpdateMap rectangles), if it does.
	const uint8_t* map = nullptr;
	size_t map_size = 0;
	uint32_t map_stride = 0;
	bool enabled = false;
};

class QemuLink {
public:
	~QemuLink();
	// Connects to QEMU's QMP socket (retrying while QEMU starts), attaches as the D-Bus display
	// client and registers the listener on console 0.
	bool connect(const std::string& qmp_path, int timeout_ms, std::string* error);
	// Runs pending D-Bus work on our side. Non-blocking. The listener's calls do not wait for it: they
	// arrive on a thread of their own, which answers each as soon as its rectangle is copied, because
	// QEMU holds the guest's GPU until then (it writes its next frame into the memory we copy from).
	void pump();
	// The scanout changes on the listener's thread: hold scanout_mutex() while reading it.
	Scanout& scanout() { return scanout_; }
	std::mutex& scanout_mutex() { return scanout_mutex_; }
	// The finger: the tablet's absolute position in guest pixels, and its left button held or let go.
	void finger_move(double x, double y);
	void finger(bool down);
	void key(uint32_t qnum, bool down);  // QEMU qnum (xtkbd) code

private:
	friend struct ListenerCalls;
	bool qmp(const std::string& json, std::string* reply, int fd = -1);
	int qmp_ = -1;
	std::string qmp_buf_;
	GDBusConnection* display_ = nullptr;   // to QEMU's org.qemu.Display1 objects
	GDBusConnection* listener_ = nullptr;  // QEMU calls our Listener on this one
	unsigned listener_id_ = 0, map_id_ = 0;
	GMainContext* listener_context_ = nullptr;  // the listener's calls are dispatched here, on listener_thread_
	std::thread listener_thread_;
	std::atomic<bool> stopping_{false};
	std::mutex scanout_mutex_;
	Scanout scanout_;
};

}  // namespace emu
