#include "qemu_link.h"

#include <sys/mman.h>
#include <gio/gunixfdlist.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>

#include <gio/gio.h>
#include <gio/gunixfdlist.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

namespace emu {

namespace {

constexpr const char* kConsole = "/org/qemu/Display1/Console_0";

// What QEMU calls on us (ui/dbus-display1.xml). Only the base interface: without Unix.Map QEMU
// sends 2D frames (ramfb's) as data. The dmabuf methods are part of it but never come for ramfb.
constexpr const char* kListenerXml = R"(<node>
 <interface name="org.qemu.Display1.Listener">
  <method name="Scanout"><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="ay" direction="in"/></method>
  <method name="Update"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="ay" direction="in"/></method>
  <method name="ScanoutDMABUF"><arg type="h" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="t" direction="in"/><arg type="b" direction="in"/></method>
  <method name="UpdateDMABUF"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Disable"/>
  <method name="MouseSet"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="CursorDefine"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="ay" direction="in"/></method>
  <property name="Interfaces" type="as" access="read"/>
 </interface>
</node>)";

// The picture as shared memory: QEMU hands its memfd over once and then says which rectangle changed, where the
// base interface carries every frame's pixels over the socket (with the GPU's frames, 11 MB each, the guest
// waiting). QEMU uses it when the Listener lists it in Interfaces and the picture is shareable (our QEMU's).
constexpr const char* kMapXml = R"(<node>
 <interface name="org.qemu.Display1.Listener.Unix.Map">
  <method name="ScanoutMap"><arg type="h" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/></method>
  <method name="UpdateMap"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
 </interface>
</node>)";

void unmap(Scanout& s) {
	if (s.map) munmap(const_cast<uint8_t*>(s.map), s.map_size);
	s.map = nullptr, s.map_size = 0;
}

// Rows [y, y + h) x [x, x + w) of the shared picture into our packed copy.
void copy_from_map(Scanout& s, uint32_t x, uint32_t y, uint32_t w, uint32_t h) {
	if (!s.map || uint64_t(x) + w > s.width || uint64_t(y) + h > s.height) return;
	for (uint32_t r = 0; r < h; ++r)
		std::memcpy(&s.pixels[(size_t(y + r) * s.width + x) * 4], s.map + size_t(y + r) * s.map_stride + size_t(x) * 4,
		            size_t(w) * 4);
}

GDBusConnection* peer(int fd, GDBusConnectionFlags extra, std::string* error) {
	GError* err = nullptr;
	GSocket* sock = g_socket_new_from_fd(fd, &err);
	if (!sock) {
		*error = std::string("socket: ") + err->message;
		g_error_free(err);
		close(fd);
		return nullptr;
	}
	GSocketConnection* stream = g_socket_connection_factory_create_connection(sock);
	g_object_unref(sock);
	// QEMU is the authentication server on both connections; we are the client.
	GDBusConnection* conn = g_dbus_connection_new_sync(
		G_IO_STREAM(stream), nullptr, GDBusConnectionFlags(G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT | extra), nullptr,
		nullptr, &err);
	g_object_unref(stream);
	if (!conn) {
		*error = std::string("D-Bus peer: ") + err->message;
		g_error_free(err);
	}
	return conn;
}

}  // namespace

struct ListenerCalls {
	static void method(GDBusConnection*, const char*, const char*, const char*, const char* name, GVariant* args,
	                   GDBusMethodInvocation* inv, gpointer self) {
		Scanout& s = static_cast<QemuLink*>(self)->scanout_;
		std::unique_lock<std::mutex> lock(static_cast<QemuLink*>(self)->scanout_mutex_);
		std::string m = name;
		static const bool debug = std::getenv("EMU_DEBUG");
		if (debug) {
			gchar* a = g_variant_print(args, FALSE);
			std::fprintf(stderr, "emu: listener %s%.120s\n", name, a);
			g_free(a);
		}
		if (m == "ScanoutMap") {
			int32_t index;
			uint32_t offset, w, h, stride, format;
			g_variant_get(args, "(huuuuu)", &index, &offset, &w, &h, &stride, &format);
			GUnixFDList* fds = g_dbus_message_get_unix_fd_list(g_dbus_method_invocation_get_message(inv));
			int fd = fds ? g_unix_fd_list_get(fds, index, nullptr) : -1;
			unmap(s);
			if (fd >= 0) {
				size_t size = size_t(offset) + size_t(stride) * h;
				void* p = mmap(nullptr, size, PROT_READ, MAP_SHARED, fd, 0);
				close(fd);
				if (p != MAP_FAILED) {
					s.map = static_cast<const uint8_t*>(p) + offset, s.map_size = size, s.map_stride = stride;
					s.width = w, s.height = h;
					s.pixels.assign(size_t(w) * h * 4, 0);
					copy_from_map(s, 0, 0, w, h);
					s.enabled = true, ++s.serial;
				}
			}
		} else if (m == "UpdateMap") {
			int32_t x, y, w, h;
			g_variant_get(args, "(iiii)", &x, &y, &w, &h);
			if (x >= 0 && y >= 0 && w > 0 && h > 0) {
				copy_from_map(s, uint32_t(x), uint32_t(y), uint32_t(w), uint32_t(h));
				++s.serial;
			}
		} else if (m == "Scanout" || m == "Update") {
			if (m == "Scanout") unmap(s);
			int32_t x = 0, y = 0;
			uint32_t w, h, stride, format;
			GVariant* data;
			if (m == "Scanout")
				g_variant_get(args, "(uuuu@ay)", &w, &h, &stride, &format, &data);
			else
				g_variant_get(args, "(iiiiuu@ay)", &x, &y, &w, &h, &stride, &format, &data);
			gsize n;
			auto* src = static_cast<const uint8_t*>(g_variant_get_fixed_array(data, &n, 1));
			if (m == "Scanout") {
				s.width = w, s.height = h;
				s.pixels.assign(size_t(w) * h * 4, 0);
			}
			// Copy row by row into our packed buffer (QEMU's stride can be wider than the rect).
			if (uint64_t(x) + w <= s.width && uint64_t(y) + h <= s.height && n >= size_t(stride) * (h ? h - 1 : 0) + size_t(w) * 4)
				for (uint32_t r = 0; r < h; ++r)
					std::memcpy(&s.pixels[(size_t(y + r) * s.width + x) * 4], src + size_t(r) * stride, size_t(w) * 4);
			g_variant_unref(data);
			s.enabled = true, ++s.serial;
		} else if (m == "Disable") {
			// Kept as it is: QEMU disables the console for an instant every time the scanout moves to
			// another resource (the guest's buffers in turn), and a blank there is a flicker each frame.
			// A panel keeps its last picture too.
		}
				lock.unlock();
				g_dbus_method_invocation_return_value(inv, nullptr);
	}
	static GVariant* property(GDBusConnection*, const char*, const char*, const char*, const char*, GError**, gpointer) {
		const char* interfaces[] = {"org.qemu.Display1.Listener.Unix.Map", nullptr};
		return g_variant_new_strv(interfaces, 1);
	}
};

QemuLink::~QemuLink() {
	if (listener_thread_.joinable()) {
		stopping_ = true;
		g_main_context_wakeup(listener_context_);
		listener_thread_.join();
	}
	if (listener_ && listener_id_) g_dbus_connection_unregister_object(listener_, listener_id_);
	if (listener_ && map_id_) g_dbus_connection_unregister_object(listener_, map_id_);
	unmap(scanout_);
	if (listener_) g_object_unref(listener_);
	if (display_) g_object_unref(display_);
	if (listener_context_) g_main_context_unref(listener_context_);
	if (qmp_ >= 0) close(qmp_);
}

bool QemuLink::qmp(const std::string& json, std::string* reply, int fd) {
	std::string line = json + "\n";
	iovec iov{line.data(), line.size()};
	msghdr msg{};
	msg.msg_iov = &iov, msg.msg_iovlen = 1;
	alignas(cmsghdr) char ctl[CMSG_SPACE(sizeof(int))];
	if (fd >= 0) {
		msg.msg_control = ctl, msg.msg_controllen = sizeof ctl;
		cmsghdr* c = CMSG_FIRSTHDR(&msg);
		c->cmsg_level = SOL_SOCKET, c->cmsg_type = SCM_RIGHTS, c->cmsg_len = CMSG_LEN(sizeof(int));
		std::memcpy(CMSG_DATA(c), &fd, sizeof fd);
	}
	if (sendmsg(qmp_, &msg, MSG_NOSIGNAL) != ssize_t(line.size())) return false;
	// Replies are one JSON object per line; skip asynchronous events until the reply arrives.
	for (;;) {
		size_t nl;
		while ((nl = qmp_buf_.find('\n')) != std::string::npos) {
			std::string obj = qmp_buf_.substr(0, nl);
			qmp_buf_.erase(0, nl + 1);
			if (obj.find("\"return\"") != std::string::npos || obj.find("\"error\"") != std::string::npos) {
				if (reply) *reply = obj;
				return obj.find("\"error\"") == std::string::npos;
			}
		}
		char buf[4096];
		ssize_t n = read(qmp_, buf, sizeof buf);
		if (n <= 0) return false;
		qmp_buf_.append(buf, size_t(n));
	}
}

bool QemuLink::connect(const std::string& qmp_path, int timeout_ms, std::string* error) {
	sockaddr_un addr{};
	addr.sun_family = AF_UNIX;
	if (qmp_path.size() >= sizeof addr.sun_path) return *error = "QMP path too long", false;
	std::memcpy(addr.sun_path, qmp_path.c_str(), qmp_path.size() + 1);
	auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
	for (;;) {
		qmp_ = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
		if (::connect(qmp_, reinterpret_cast<sockaddr*>(&addr), sizeof addr) == 0) break;
		close(qmp_), qmp_ = -1;
		if (std::chrono::steady_clock::now() > deadline) return *error = qmp_path + ": " + std::strerror(errno), false;
		std::this_thread::sleep_for(std::chrono::milliseconds(50));
	}
	std::string reply;
	if (!qmp(R"({"execute":"qmp_capabilities"})", &reply)) return *error = "QMP handshake failed: " + reply, false;

	// One end of a socketpair goes to QEMU as its D-Bus display client; we talk on the other.
	int sv[2];
	if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, sv) != 0) return *error = "socketpair failed", false;
	bool sent = qmp(R"({"execute":"getfd","arguments":{"fdname":"emu"}})", &reply, sv[1]);
	close(sv[1]);
	if (!sent || !qmp(R"({"execute":"add_client","arguments":{"protocol":"@dbus-display","fdname":"emu"}})", &reply)) {
		close(sv[0]);
		return *error = "QEMU refused the display client (is it running with -display dbus,p2p=yes?): " + reply, false;
	}
	if (!(display_ = peer(sv[0], G_DBUS_CONNECTION_FLAGS_NONE, error))) return false;

	// RegisterListener hands QEMU a second socket on which it calls us. It replies first and
	// then authenticates, so our side of that connection is made after the call returns, with
	// message processing held until the Listener object is in place.
	int lv[2];
	if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, lv) != 0) return *error = "socketpair failed", false;
	GUnixFDList* fds = g_unix_fd_list_new_from_array(&lv[1], 1);  // takes lv[1]
	GError* err = nullptr;
	GVariant* r = g_dbus_connection_call_with_unix_fd_list_sync(display_, nullptr, kConsole, "org.qemu.Display1.Console",
	                                                            "RegisterListener", g_variant_new("(h)", 0), nullptr,
	                                                            G_DBUS_CALL_FLAGS_NONE, 5000, fds, nullptr, nullptr, &err);
	g_object_unref(fds);
	if (!r) {
		*error = std::string("RegisterListener: ") + err->message;
		g_error_free(err);
		close(lv[0]);
		return false;
	}
	g_variant_unref(r);
	if (!(listener_ = peer(lv[0], G_DBUS_CONNECTION_FLAGS_DELAY_MESSAGE_PROCESSING, error))) return false;
	// The objects' calls are dispatched in the thread-default context of the thread that registers them.
	listener_context_ = g_main_context_new();
	g_main_context_push_thread_default(listener_context_);
	GDBusNodeInfo* node = g_dbus_node_info_new_for_xml(kListenerXml, nullptr);
	static const GDBusInterfaceVTable vtable{ListenerCalls::method, ListenerCalls::property, nullptr, {}};
	listener_id_ = g_dbus_connection_register_object(listener_, "/org/qemu/Display1/Listener", node->interfaces[0], &vtable,
	                                                 this, nullptr, &err);
	g_dbus_node_info_unref(node);
	if (!listener_id_) {
		*error = std::string("register Listener: ") + err->message;
		g_error_free(err);
		g_main_context_pop_thread_default(listener_context_);
		return false;
	}
	{
		GDBusNodeInfo* map_node = g_dbus_node_info_new_for_xml(kMapXml, nullptr);
		map_id_ = g_dbus_connection_register_object(listener_, "/org/qemu/Display1/Listener", map_node->interfaces[0], &vtable,
		                                            this, nullptr, nullptr);
		g_dbus_node_info_unref(map_node);
	}
	g_main_context_pop_thread_default(listener_context_);
	g_dbus_connection_start_message_processing(listener_);
	listener_thread_ = std::thread([this] {
		while (!stopping_) g_main_context_iteration(listener_context_, TRUE);
	});
	return true;
}

void QemuLink::pump() {
	while (g_main_context_iteration(nullptr, FALSE)) {
	}
}

void QemuLink::finger_move(double x, double y) {
	g_dbus_connection_call(display_, nullptr, kConsole, "org.qemu.Display1.Mouse", "SetAbsPosition",
	                       g_variant_new("(uu)", uint32_t(std::max(0.0, x)), uint32_t(std::max(0.0, y))), nullptr,
	                       G_DBUS_CALL_FLAGS_NONE, -1, nullptr, nullptr, nullptr);
}

void QemuLink::finger(bool down) {
	g_dbus_connection_call(display_, nullptr, kConsole, "org.qemu.Display1.Mouse", down ? "Press" : "Release",
	                       g_variant_new("(u)", 0u /* INPUT_BUTTON_LEFT */), nullptr, G_DBUS_CALL_FLAGS_NONE, -1, nullptr,
	                       nullptr, nullptr);
}

void QemuLink::key(uint32_t qnum, bool down) {
	g_dbus_connection_call(display_, nullptr, kConsole, "org.qemu.Display1.Keyboard", down ? "Press" : "Release",
	                       g_variant_new("(u)", qnum), nullptr, G_DBUS_CALL_FLAGS_NONE, -1, nullptr, nullptr, nullptr);
}

}  // namespace emu
