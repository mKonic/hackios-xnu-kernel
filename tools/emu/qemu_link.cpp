#include "qemu_link.h"

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
		std::string m = name;
		static const bool debug = std::getenv("EMU_DEBUG");
		if (debug) {
			gchar* a = g_variant_print(args, FALSE);
			std::fprintf(stderr, "emu: listener %s%.120s\n", name, a);
			g_free(a);
		}
		if (m == "Scanout" || m == "Update") {
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
			s.enabled = false, ++s.serial;
		}
				g_dbus_method_invocation_return_value(inv, nullptr);
	}
	static GVariant* property(GDBusConnection*, const char*, const char*, const char*, const char*, GError**, gpointer) {
		const char* none[] = {nullptr};
		return g_variant_new_strv(none, 0);
	}
};

QemuLink::~QemuLink() {
	if (listener_ && listener_id_) g_dbus_connection_unregister_object(listener_, listener_id_);
	if (listener_) g_object_unref(listener_);
	if (display_) g_object_unref(display_);
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
	GDBusNodeInfo* node = g_dbus_node_info_new_for_xml(kListenerXml, nullptr);
	static const GDBusInterfaceVTable vtable{ListenerCalls::method, ListenerCalls::property, nullptr, {}};
	listener_id_ = g_dbus_connection_register_object(listener_, "/org/qemu/Display1/Listener", node->interfaces[0], &vtable,
	                                                 this, nullptr, &err);
	g_dbus_node_info_unref(node);
	if (!listener_id_) {
		*error = std::string("register Listener: ") + err->message;
		g_error_free(err);
		return false;
	}
	g_dbus_connection_start_message_processing(listener_);
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
