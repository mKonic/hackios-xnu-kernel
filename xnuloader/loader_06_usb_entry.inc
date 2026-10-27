/* ------------------------------------------------------------------------ */
/* USB command channel: a small fastboot-protocol server.                    */
/*                                                                          */
/* Host side (stock `fastboot` tool):                                        */
/*   fastboot stage payload.bin    download a new "XNUP" payload            */
/*   fastboot oem run [N]          run its kernel (optionally stop at N)    */
/*   fastboot getvar result        blocks until the kernel is back          */
/*   fastboot get_staged log.txt   fetch loader + kernel log                */
/*   fastboot oem hang             stop petting the watchdog (test)         */
/*   fastboot reboot               cold reset                               */

#define USB_EP_OUT   0x01
#define USB_EP_IN    0x81
#define USB_CHUNK    (1024 * 1024)

static usb_device_protocol *USB;
static int download_to_ramdisk;     /* set by "oem ramdisk" for the one download that follows */
static uint8_t *usb_rx_buf, *usb_tx_buf;
static int usb_connected;

static const uint8_t usb_dev_desc[18] = { 18, 1, 0x10, 0x02, 0, 0, 0, 64, 0xd1, 0x18, 0x0d, 0xd0, 0x00, 0x01, 1, 2, 3, 1 };
static const uint8_t usb_ss_dev_desc[18] = { 18, 1, 0x00, 0x03, 0, 0, 0, 9, 0xd1, 0x18, 0x0d, 0xd0, 0x00, 0x01, 1, 2, 3, 1 };
static const uint8_t usb_cfg_desc[32] = {
	9, 2, 32, 0, 1, 1, 0, 0x80, 0x50,
	9, 4, 0, 0, 2, 0xff, 0x42, 0x03, 4,   /* fastboot interface */
	7, 5, USB_EP_IN, 2, 0x00, 0x02, 0,
	7, 5, USB_EP_OUT, 2, 0x00, 0x02, 0,
};
static const uint8_t usb_ss_cfg_desc[44] = {
	9, 2, 44, 0, 1, 1, 0, 0x80, 0x10,
	9, 4, 0, 0, 2, 0xff, 0x42, 0x03, 4,
	7, 5, USB_EP_IN, 2, 0x00, 0x04, 0,   6, 0x30, 4, 0, 0, 0,
	7, 5, USB_EP_OUT, 2, 0x00, 0x04, 0,  6, 0x30, 4, 0, 0, 0,
};
static const uint8_t usb_qualifier[10] = { 10, 6, 0x00, 0x02, 0xff, 0xff, 0xff, 64, 1, 0 };
static const uint8_t usb_bos[42] = {
	5, 0x0f, 42, 0, 3,
	7, 0x10, 0x02, 0x06, 0, 0, 0,
	10, 0x10, 0x03, 0x00, 0x0e, 0x00, 0x01, 0x07, 0x65, 0x00,
	20, 0x10, 0x0a, 0x00, 0x01, 0, 0, 0, 0x00, 0x11, 0, 0, 0x30, 0x40, 0x0a, 0x00, 0xb0, 0x40, 0x0a, 0x00,
};
static const uint8_t usb_str0[4] = { 4, 3, 0x09, 0x04 };
static const uint8_t usb_str_mfr[8] = { 8, 3, 'X', 0, 'N', 0, 'U', 0 };
static const uint8_t usb_str_prod[20] = { 20, 3, 'x', 0, 'n', 0, 'u', 0, 'l', 0, 'o', 0, 'a', 0, 'd', 0, 'e', 0, 'r', 0 };
static const uint8_t usb_str_serial[20] = { 20, 3, 'x', 0, 'n', 0, 'u', 0, 'l', 0, 'o', 0, 'a', 0, 'd', 0, 'e', 0, 'r', 0 };
static const uint8_t usb_str_iface[18] = { 18, 3, 'f', 0, 'a', 0, 's', 0, 't', 0, 'b', 0, 'o', 0, 'o', 0, 't', 0 };

static void EFIAPI
usb_dummy_notify(void *event, void *ctx)
{
}

static int
usb_start(void)
{
	static const efi_guid usb_guid = QCOM_USB_DEVICE_GUID;
	static const efi_guid init_guid = QCOM_USB_INIT_EVENT_GUID;
	static const void *cfg = usb_cfg_desc, *ss_cfg = usb_ss_cfg_desc;
	static const void *strings[5] = { usb_str0, usb_str_mfr, usb_str_prod, usb_str_serial, usb_str_iface };
	static usb_device_descriptor_set set;
	void *event;

	/* Ask the platform to bring the controller up in peripheral mode (as the stock fastboot does). */
	if (!EFI_ERROR(BS->create_event_ex(0x200 /* EVT_NOTIFY_SIGNAL */, 8 /* TPL_CALLBACK */, usb_dummy_notify, NULL,
	    &init_guid, &event))) {
		BS->signal_event(event);
		BS->close_event(event);
	}
	if (EFI_ERROR(BS->locate_protocol(&usb_guid, NULL, (void **)&USB))) {
		print("usb: device protocol not found\n");
		return 0;
	}
	set.device_descriptor = usb_dev_desc;
	set.descriptors = &cfg;
	set.ss_device_descriptor = usb_ss_dev_desc;
	set.ss_descriptors = &ss_cfg;
	set.device_qualifier = usb_qualifier;
	set.bos = usb_bos;
	set.string_count = 5;
	set.strings = strings;
	efi_status s = USB->start_ex(&set);
	if (EFI_ERROR(s)) {
		print("usb: start failed %x\n", s);
		return 0;
	}
	if (EFI_ERROR(USB->allocate_transfer_buffer(USB_CHUNK, (void **)&usb_rx_buf)) ||
	    EFI_ERROR(USB->allocate_transfer_buffer(USB_CHUNK, (void **)&usb_tx_buf))) {
		print("usb: no transfer buffers\n");
		return 0;
	}
	print("usb: started\n");
	return 1;
}

/*
 * Pump controller events until a transfer on `endpoint` finishes.
 * Returns the byte count, or -1 if the transfer was cancelled or the cable
 * state changed. Keeps the watchdog fed while idle.
 */
/* LC-USB-BEGIN -- extracted verbatim by tools/test-loader-lifecycle.py.
 * Each endpoint has one owned request. Only a successful, full-sized transfer event with
 * the recorded buffer and a byte count within its submitted length may end that request.
 * Active/unknown statuses keep ownership; malformed records and CompleteError isolate the
 * endpoint until reset. Cancelled releases without delivery; CompleteOK may deliver bytes.
 * Disconnects invalidate owned requests. Timeouts invalidate IN replies and OUT data, while
 * OUT command receives remain valid across idle polls. A command completed during an IN
 * wait retains its buffer until usb_rx or usb_poll_command delivers it exactly once.
 * All waits have a three-second bound in running/held/fault states and ten seconds otherwise.
 * A stale IN request may request one abort; its return never releases the buffer. The
 * shared event verifier handles subsequent terminals while OUT commands remain usable.
 * efi.h has no request id: attribution assumes one unfinished request per endpoint and one
 * terminal per request. Firmware behavior still requires device validation. */
static int usb_rx_queued;       /* usb_poll_command() has left the receive for the next command with the controller */
static int usb_out_owned;       /* the OUT buffer is with the controller, its end unconfirmed */
static int usb_in_owned;        /* the IN buffer is with the controller, its end unconfirmed */
static int usb_out_lost;        /* structurally isolated: an external reset is the recovery boundary */
static int usb_in_lost;
static void *usb_out_buf;       /* what the owned request submitted, kept to verify the notification */
static uint64_t usb_out_len;
static int64_t usb_command_bytes = -1;   /* OUT purpose/result: -1 data, 0 waiting command, >0 completed command */
static void *usb_in_buf;
static uint64_t usb_in_len;
static int usb_out_invalid;     /* the owned request was left by a timeout or a disconnect: its terminal may
                                 * still come, but its bytes may no longer be handed to a caller */
static int usb_in_invalid;
static uint64_t usb_in_since;   /* submit time: elapsed time permits an abort request, never buffer reuse */
static int usb_in_abort_requested;   /* at most one abort for the recorded request */
static int usb_in_recoveries;   /* bounds firmware abort calls across this loader session */

static void
usb_stop_endpoint(int out, const char *why)
{
	if (out) {
		if (!usb_out_lost) {
			print("usb: OUT %s: the buffer stays with the controller, reboot to start over\n", why);
			usb_out_lost = 1;
		}
	} else if (!usb_in_lost) {
		print("usb: IN %s: the buffer stays with the controller, reboot to start over\n", why);
		usb_in_lost = 1;
	}
}

/* a transfer event below the transfer struct cannot name an endpoint or a buffer: with one
 * on hand the loader keeps no in-flight claim it cannot still account for */
static void
usb_short_event_stops_all(void)
{
	if (usb_out_owned || usb_rx_queued) {
		usb_stop_endpoint(1, "event too short to attribute");
	}
	if (usb_in_owned) {
		usb_stop_endpoint(0, "event too short to attribute");
	}
}

/* the shared rule for usb_wait and usb_poll_command: verify one transfer notification against
 * the endpoint's recorded request before anything is reused, and the caller has proven the
 * event carries a full transfer struct. 1: verified CompleteOK of a deliverable request,
 * record released, *bytes is the answer; 2: verified Cancelled, record released as a failure
 * without bytes; 3: the terminal of a request a timeout or a disconnect had marked
 * undeliverable: record released, *bytes unread, the wait ends without a delivery; 0: keep
 * everything as it is (isolated, no recorded request, Active, undocumented value); -1: the
 * endpoint was structurally isolated and its buffer stays with the controller */
static int
usb_notification(int out, const usb_device_event_data *data, uint64_t *bytes)
{
	void *sub = out ? usb_out_buf : usb_in_buf;
	uint64_t sub_len = out ? usb_out_len : usb_in_len;

	if (out ? usb_out_lost : usb_in_lost) {
		return 0;   /* isolated keeps its buffer and its flags: a late event changes nothing */
	}
	if (out ? !usb_out_owned : !usb_in_owned) {
		return 0;   /* nothing on file: a late duplicate of an old request changes nothing */
	}
	if (sub == NULL) {
		usb_stop_endpoint(out, "request on file without its submitted buffer");
		return -1;
	}
	if (data->transfer.data_buffer != sub) {
		usb_stop_endpoint(out, "notification for a buffer the loader did not submit");
		return -1;
	}
	if (data->transfer.status == UsbDeviceTransferStatusActive ||
	    data->transfer.status > UsbDeviceTransferStatusCompleteError) {
		return 0;   /* the controller may still work on the buffer: no read, no release, no resend */
	}
	if (data->transfer.status == UsbDeviceTransferStatusCancelled) {
		if (data->transfer.bytes_completed > sub_len) {
			usb_stop_endpoint(out, "Cancelled over a length that was not submitted");
			return -1;   /* overlong like an over-OK: structurally unsound, nothing releases */
		}
		if (out) {
			usb_out_owned = 0;    /* the request ended as a failure: its record ends with it */
			usb_rx_queued = 0;
			usb_out_invalid = 0;
		} else {
			usb_in_owned = 0;
			usb_in_invalid = 0;
		}
		return 2;   /* no success bytes and no dispatch; a later normal submit may go */
	}
	if (data->transfer.status != UsbDeviceTransferStatusCompleteOK) {
		usb_stop_endpoint(out, "CompleteError with buffer-return semantics unproven");
		return -1;
	}
	if (data->transfer.bytes_completed > sub_len) {
		usb_stop_endpoint(out, "completed more bytes than submitted");
		return -1;
	}
	*bytes = data->transfer.bytes_completed;
	if (out) {
		usb_out_owned = 0;    /* this notification is the firmware's confirmation of the end */
		usb_rx_queued = 0;    /* the queue's claim on the buffer ends with the transfer it covered */
		if (usb_out_invalid) {
			usb_out_invalid = 0;
			return 3;       /* a timeout or disconnect already ended the caller's interest */
		}
	} else {
		usb_in_owned = 0;
		if (usb_in_invalid) {
			usb_in_invalid = 0;
			return 3;
		}
	}
	return 1;
}

/* A stale reply may be aborted, but only a verified terminal can release its buffer.
 * Event handling stays in usb_wait/usb_poll_command so an OUT completion cannot be lost
 * while the IN abort is pending. A request with no terminal remains owned until reset. */
#define USB_IN_STALE_SECONDS    5
#define USB_IN_RECOVER_LIMIT    3

static int
usb_abort_stale_in(const char *why)
{
	uint64_t freq = READ_SYSREG(CNTFRQ_EL0);
	uint64_t age;

	if (USB == NULL || !usb_in_owned) {
		return 0;
	}
	if (usb_in_lost || usb_in_abort_requested) {
		return -1;
	}
	age = usb_in_since ? (READ_SYSREG(CNTVCT_EL0) - usb_in_since) / (freq ? freq : 1) : 0;
	if (!usb_in_invalid && usb_in_since != 0 && age < USB_IN_STALE_SECONDS) {
		return -1;      /* young: its transfer may still be running, nothing to recover */
	}
	if (usb_in_recoveries >= USB_IN_RECOVER_LIMIT || USB->abort_xfer == NULL) {
		return -1;
	}
	usb_in_recoveries++;
	usb_in_abort_requested = 1;
	(void)USB->abort_xfer(USB_EP_IN);
	print("usb: IN abort requested before %s after %d s (attempt %d); awaiting verified terminal\n",
	    why, age, (uint64_t)usb_in_recoveries);
	return -1;
}

#define USB_IDLE_WAIT_SECONDS   10  /* a plain command loop: long enough for one staged megabyte */

static int64_t
usb_wait(uint8_t endpoint, int deliver)
{
	/* static: a long transfer is many short waits, and the watchdog must be fed across them */
	static uint64_t last_pet;
	uint64_t freq = READ_SYSREG(CNTFRQ_EL0), began = READ_SYSREG(CNTVCT_EL0);

	for (;;) {
		uint32_t msg = UsbDeviceEventNoEvent;
		uint64_t size = 0, bytes = 0;
		usb_device_event_data data;

		if (EFI_ERROR(USB->handle_event(&msg, &size, &data))) {
			msg = UsbDeviceEventNoEvent;   /* a failed call delivered no event to read */
		}
		/* a completion in hand is verified before the budget: one arriving at the deadline
		 * must not be thrown away; every path that keeps waiting passes the budget check */
		if (msg == UsbDeviceEventDeviceStateChange) {
			/* no union field is read before the event's own payload was size-checked */
			if (size >= sizeof(data.device_state)) {
				int was_connected = usb_connected;
				usb_connected = (data.device_state == UsbDeviceStateConnected);
				if (data.device_state != UsbDeviceStateConnected) {
					if (usb_command_bytes > 0) {
						usb_command_bytes = 0;
					}
					/* a disconnect ends this wait; the records stay on file, marked so a
					 * terminal that still arrives only releases them, delivers nothing */
					if (usb_out_owned) {
						usb_out_invalid = 1;
					}
					if (usb_in_owned) {
						usb_in_invalid = 1;
					}
					return -1;
				}
				if (!was_connected && endpoint == 0xff) {
					/* a link wait exists to learn of the connection: the command loop must
					 * return and submit, holding every record exactly as it stands */
					return 0;
				}
			}
			/* a reconnect on an already-up link or an unreadable state says nothing about a
			 * transfer: keep waiting, resend nothing, clear nothing */
		} else if (msg == UsbDeviceEventTransferNotification) {
			if (size < sizeof(data.transfer)) {
				usb_short_event_stops_all();   /* nothing readable names the request it ends */
				return -1;
			}
			if (data.transfer.endpoint_index == USB_EP_OUT || data.transfer.endpoint_index == USB_EP_IN) {
				/* the other endpoint's terminal is booked here so its record finishes
				 * even while this side waits */
				int kept = usb_notification(data.transfer.endpoint_index == USB_EP_OUT, &data, &bytes);
				if (data.transfer.endpoint_index == USB_EP_OUT && endpoint != USB_EP_OUT &&
				    kept == 1 && usb_command_bytes == 0) {
					usb_command_bytes = (int64_t)bytes;
				}
				if (data.transfer.endpoint_index == endpoint) {
					if (kept == 1 && deliver) {
						return (int64_t)bytes;
					}
					if (kept != 0) {
						/* recycled without a valid wait, ended as a failure, or isolated */
						return -1;
					}
				}
			}
		}
		/* the wait is bounded in every state: a host that went away must never keep this core
		 * from reaching the commands that end the state. The held, running and fault states
		 * keep the three-second rule they had, because those are the states a command may have
		 * to end; a plain command loop gets the longer limit, which is still short enough to
		 * serve a reboot and long enough for one staged megabyte to run to its end. */
		uint64_t limit = (running_live != NULL || kernel_resources_held || firmware_fault)
		    ? 3 * freq : USB_IDLE_WAIT_SECONDS * freq;
		if (READ_SYSREG(CNTVCT_EL0) - began > limit) {
			/* A command receive stays valid across idle polls; the command loop resumes the
			 * same request. Abandoned data receives and IN replies only release late bytes. */
			if (usb_out_owned && usb_command_bytes < 0) {
				usb_out_invalid = 1;
			}
			if (usb_in_owned) {
				usb_in_invalid = 1;
			}
			return -1;   /* both endpoints stay owned: a later terminal may still finish them */
		}
		if (READ_SYSREG(CNTVCT_EL0) - last_pet > freq) {
			last_pet = READ_SYSREG(CNTVCT_EL0);
			wdt_pet();
		}
	}
}

static int64_t
usb_rx(uint8_t *buf, uint64_t len, int command_wait)
{
	/* Request an IN abort before waiting for the next OUT command. Its terminal is handled
	 * by the same wait as other transfer events; no buffer changes at this boundary. */
	(void)usb_abort_stale_in("the next command");
	if (usb_command_bytes > 0) {
		if (!command_wait || buf != usb_out_buf || len != usb_out_len) {
			return -1;
		}
		int64_t n = usb_command_bytes;
		usb_command_bytes = 0;
		return n;
	}
	if (usb_out_lost) {
		usb_wait(0xff, 0);    /* bounded and watchdog-feeding, so the caller's loop cannot spin */
		return -1;            /* the isolated channel keeps its buffer: no new send, no overwrite */
	}
	if (usb_rx_queued) {
		/* the pending request stays exactly as it was submitted: only a caller using the
		 * recorded buffer and length may take it over */
		if (buf != usb_out_buf || len != usb_out_len) {
			return -1;      /* refused: the on-file request and its ownership are untouched */
		}
		usb_rx_queued = 0;      /* (every command is received the same way, into the same buffer) */
	} else {
		if (usb_out_owned) {
			/* the record alone says the buffer is with the controller (a timeout or a
			 * disconnect left it there): sending again would hand the same buffer twice */
			return usb_wait(USB_EP_OUT, 1);   /* wait out the terminal that will release it */
		}
		if (EFI_ERROR(USB->send(USB_EP_OUT, len, buf))) {
			return -1;
		}
		usb_out_owned = 1;      /* the submit record is written only for a send that succeeded */
		usb_out_buf = buf;      /* the notification is only believed for this buffer and length */
		usb_out_len = len;
		usb_command_bytes = command_wait ? 0 : -1;
		usb_out_invalid = 0;    /* a new request starts with a fresh delivery state */
	}
	return usb_wait(USB_EP_OUT, 1);
}

static int64_t
usb_tx(const uint8_t *data, uint64_t len)
{
	if (usb_in_lost) {
		return -1;          /* isolated: no memcpy over the buffer the controller kept, no send */
	}
	if (usb_in_owned && usb_abort_stale_in("a reply") < 0) {
		return -1;          /* the old send still holds the buffer: no memcpy over it, no send */
	}
	if (data != usb_tx_buf) {
		memcpy(usb_tx_buf, data, len);
	}
	if (EFI_ERROR(USB->send(USB_EP_IN, len, usb_tx_buf))) {
		return -1;
	}
	usb_in_owned = 1;
	usb_in_since = READ_SYSREG(CNTVCT_EL0);
	usb_in_abort_requested = 0;
	usb_in_buf = usb_tx_buf;    /* the notification is only believed for this buffer and length */
	usb_in_len = len;
	usb_in_invalid = 0;         /* a new request starts with a fresh delivery state */
	return usb_wait(USB_EP_IN, 1);
}
/* LC-USB-END */

/* LC-FB-BEGIN -- extracted verbatim by tools/test-loader-lifecycle.py: both reply forms pack
 * into their own stack buffers, so usb_tx stays the one place allowed to touch usb_tx_buf
 * (rev3 review: a reply written while the IN transfer was owned or lost overwrote the
 * controller's buffer before usb_tx's guard ran). Both loops carry a hard limit. */
static void
fb_reply(const char *code, const char *text)
{
	char msg[64];
	uint32_t n = 0;

	for (; *code && n < sizeof(msg) - 1; code++) {
		msg[n++] = (uint8_t)*code;
	}
	for (; *text && n < sizeof(msg) - 1; text++) {
		msg[n++] = (uint8_t)*text;
	}
	usb_tx((const uint8_t *)msg, n);
}

static void
fb_reply_data(uint64_t len)
{
	char msg[13] = "DATA00000000";
	for (int i = 0; i < 8; i++) {
		msg[4 + i] = "0123456789abcdef"[(len >> (28 - 4 * i)) & 0xf];
	}
	usb_tx((const uint8_t *)msg, 12);
}
/* LC-FB-END */

/* LC-BID-BEGIN -- extracted verbatim by tools/test-loader-buildid.py: what the image says it
 * is. LOADER_BUILD_ID names the source commit the build was made from: build.sh passes a full
 * 40-hex BUILD_ID in from outside the source, the build manifest pairs that value with the
 * artifact hash recorded after the link. The artifact hash stays out of the image: a file that
 * contains its own hash changes that hash by being written. The identifier supports what the
 * image claims to be; the manifest and the load record are what back what actually ran, and
 * neither is a cryptographic proof of the running code. An unset value reports "unknown" as
 * it is, and build.sh warns at the build that produced it. */
#ifndef LOADER_BUILD_ID
#define LOADER_BUILD_ID "unknown"   /* no build-time id was given: the answer says so */
#endif

/* the three identity variables of "fastboot getvar", answered through fb_reply: 1 when this
 * var was one of them; the reply packer fits 63 characters with the code, so a 40-hex
 * identifier sits well inside the limit */
static int
fb_getvar_identity(const char *var)
{
	if (str_eq(var, "product")) {
		fb_reply("OKAY", "xnuloader");
	} else if (str_eq(var, "version")) {
		fb_reply("OKAY", "0.5");
	} else if (str_eq(var, "build-id")) {
		fb_reply("OKAY", LOADER_BUILD_ID);
	} else {
		return 0;
	}
	return 1;
}
/* LC-BID-END */

static int
starts_with(const char *s, const char *prefix)
{
	while (*prefix) {
		if (*s++ != *prefix++) {
			return 0;
		}
	}
	return 1;
}

#include "loader_ramdisk.inc"

/*
 * One command from the host. While a kernel runs on another core (running_live) only what does
 * not disturb it: questions, the screen, the log so far, and "oem stop". While memory and devices
 * are held (kernel_resources_held) the same commands pass and the same ones are refused, with
 * nothing to notify: the transitions live in the LC / LC-ENTER / LC-WATCH / LC-RETURN /
 * LC-WRITE / LC-USB / LC-FB / LC-POLL / LC-EXC
 * blocks and are exercised by tools/test-loader-lifecycle.py.
 */
static void
fb_command(char *cmd)
{
	int live = running_live != NULL;

	{
		if (lc_cmd_denied(kernel_resources_held, firmware_fault, live, cmd)) {
			fb_reply("FAIL", live ? "a kernel is running (oem stop ends it)"
			    : "a kernel may still hold memory or devices: reboot to start over");
		} else if (ramdisk_command(cmd)) {
			/* The RAM protocol uses the same command protection as legacy downloads. */
		} else if (starts_with(cmd, "oem stop")) {
			/* the kernel looks at this once a second and comes back as it does when its time is up */
			int act = lc_stop_action(kernel_resources_held, live);

			if (act == LC_STOP_HELD) {
				fb_reply("FAIL", "no kernel to notify: memory and devices may still be held (reboot to start over)");
			} else {
				if (act == LC_STOP_NOTIFY) {
					((struct kernel_live *)(uintptr_t)running_live)->stop = 1;
				}
				fb_reply("OKAY", "");
			}
		} else if (starts_with(cmd, "getvar:")) {
			const char *var = cmd + 7;
			if (fb_getvar_identity(var)) {   /* product, version and build-id answer from one block */
			} else if (str_eq(var, "max-download-size")) {
				fb_reply("OKAY", (download_to_ramdisk || ramdisk_pending()) ? "0xffffffff" : "0x03700000");
			} else if (str_eq(var, "result")) {
				fb_reply("OKAY", last_result);
			} else if (str_eq(var, "screen") && SCREEN.base != 0) {
				/* width height bytes-per-row */
				uint32_t mark = log_len;
				print("%d %d %d", (uint64_t)SCREEN.width, (uint64_t)SCREEN.height, (uint64_t)SCREEN.stride);
				log_buf[log_len] = 0;
				fb_reply("OKAY", log_buf + mark);
				log_len = mark;
			} else {
				fb_reply("OKAY", "");
			}
		} else if (starts_with(cmd, "oem ramdisk")) {
			/* "oem ramdisk": the next download is a root image for the resident area; "oem ramdisk none" drops it */
			struct payload_info pi;

			parse_payload(&pi, 0);
			log_len = 0;
			if (!choose_window(&pi) || win_size <= RESIDENT_RD_OFFSET + RESIDENT_RD_RESERVE) {
				fb_reply("FAIL", "no memory window");
				return;
			}
			ramdisk_forget();
			download_to_ramdisk = 1;
			fb_reply("OKAY", "");
		} else if (starts_with(cmd, "download:")) {
			uint64_t total = parse_number(cmd + 9, 16), got = 0;
			uint64_t limit = PAYLOAD_CONTAINER_MAX;
			uint8_t *dest = (uint8_t *)PAYLOAD_BASE;
			int streaming = ramdisk_pending();
			int to_ramdisk = download_to_ramdisk || streaming;

			download_to_ramdisk = 0;
			if (streaming) {
				if (!ramdisk_block_size(cmd + 9, &total)) {
					fb_reply("FAIL", "ramdisk block rejected: cancel or begin again");
					return;
				}
				limit = ramdisk_transfer.total - ramdisk_transfer.received;
				dest = (uint8_t *)(win_base + RESIDENT_RD_OFFSET + ramdisk_transfer.received);
			} else if (to_ramdisk) {
				limit = win_size - RESIDENT_RD_OFFSET - RESIDENT_RD_RESERVE;
				dest = (uint8_t *)(win_base + RESIDENT_RD_OFFSET);
				resident_rd_size = 0;
			}
			if (total == 0 || total > limit || total > 0xffffffffULL) {
				fb_reply("FAIL", "bad size");
				return;
			}
			if (!to_ramdisk) {
				struct payload_header *ph = (struct payload_header *)PAYLOAD_BASE;

				/* the region holds no consumable container before this transfer does (review U1) */
				ph->magic = 0;
				ph->size = 0;
				payload_bytes = 0;
			}
			fb_reply_data(total);
			while (got < total) {
				uint64_t want = total - got > USB_CHUNK ? USB_CHUNK : total - got;
				int64_t r = usb_rx(usb_rx_buf, want, 0);
				if (r <= 0) {
					break;
				}
				memcpy(dest + got, usb_rx_buf, (size_t)r);
				got += (uint64_t)r;
			}
			if (streaming) ramdisk_block_done(total, got);
			if (got == total) {
				if (to_ramdisk && !streaming) {
					resident_rd_size = total;
				} else if (!to_ramdisk) {
					payload_bytes = total;   /* a complete container: parse may trust the header now */
				}
				fb_reply("OKAY", "");
			} else {
				if (!to_ramdisk) {
					struct payload_header *ph = (struct payload_header *)PAYLOAD_BASE;

					/* a failed transfer publishes nothing: the header its bytes wrote is
					 * erased, so even a complete-looking prefix waits for a full download */
					ph->magic = 0;
					ph->size = 0;
					payload_bytes = got;    /* the real received count stays on record */
				}
				fb_reply("FAIL", "short download");
			}
		} else if (starts_with(cmd, "upload")) {
			uint64_t sent = 0;
			/* fastboot rejects DATA00000000 without reading the final status. */
			if (staged_len == 0) {
				fb_reply("FAIL", "no staged data");
				return;
			}
			fb_reply_data(staged_len);
			while (sent < staged_len) {
				uint64_t chunk = staged_len - sent > USB_CHUNK ? USB_CHUNK : staged_len - sent;
				if (usb_tx((const uint8_t *)staged_data + sent, chunk) < 0) {
					break;
				}
				sent += chunk;
			}
			fb_reply("OKAY", "");
		} else if (starts_with(cmd, "oem screenshot")) {
			/* stage the raw framebuffer; "getvar screen" gives its geometry */
			if (SCREEN.base == 0) {
				fb_reply("FAIL", "no display");
				return;
			}
			staged_data = (const char *)(uintptr_t)SCREEN.base;
			staged_len = (uint64_t)SCREEN.stride * SCREEN.height;
			fb_reply("OKAY", "");
		} else if (starts_with(cmd, "oem recording")) {
			/* stage what the kernel's display driver recorded (boot-arg rec_secs) */
			if (recording_data == NULL) {
				fb_reply("FAIL", "no recording");
				return;
			}
			staged_data = recording_data;
			staged_len = recording_len;
			fb_reply("OKAY", "");
		} else if (starts_with(cmd, "oem sound")) {
			/* stage what the kernel recorded of the sound (boot-arg rec_sound) */
			if (sound_data == NULL) {
				fb_reply("FAIL", "no sound recording");
				return;
			}
			staged_data = sound_data;
			staged_len = sound_len;
			fb_reply("OKAY", "");
		} else if (starts_with(cmd, "oem clearscreen")) {
			if (SCREEN.base != 0) {
				memset((void *)(uintptr_t)SCREEN.base, 0, (size_t)SCREEN.stride * SCREEN.height);
			}
			fb_reply("OKAY", "");
		} else if (starts_with(cmd, "oem run") || starts_with(cmd, "oem selftest")) {
			int selftest = starts_with(cmd, "oem selftest");
			const char *arg = cmd + 7;
			while (*arg == ' ') {
				arg++;
			}
			int have_cp = selftest || (*arg >= '0' && *arg <= '9');
			uint64_t cp = selftest ? CHECKPOINT_SELFTEST : parse_number(arg, 10);

			set_result("running", cp, 0);
			fb_reply("OKAY", "");
			log_len = 0;
			print("== XNULOADER run (usb) ==\n");
			if (!run_kernel(have_cp, cp)) {
				set_result("not-started", 0, 0);
				compose_logs(NULL, 0);
			}
		} else if (starts_with(cmd, "oem hang")) {
			fb_reply("OKAY", "watchdog test: not petting any more");
			for (;;) {
			}
		} else if (starts_with(cmd, "oem memmap")) {
			/* the firmware's memory map into the log (then "oem log" and fetch it) */
			print_memory_map();
			fb_reply("OKAY", "");
		} else if (starts_with(cmd, "oem logwrite")) {
			/*
			 * The only path that lets the log land on boot_a where no logdump partition
			 * exists (review F09, ruling b); the caller knows boot_a may hold the boot image.
			 */
			fb_reply("OKAY", "");
			log_fallback_boot_a_allowed = 1;
			flush_log();
			log_fallback_boot_a_allowed = 0;
		} else if (starts_with(cmd, "oem probe")) {
			/* the only writable storage check, and it puts back what it moved (review F09) */
			fb_reply("OKAY", "");
			probe_boot_a();
		} else if (starts_with(cmd, "oem log")) {
			if (running_live != NULL) {
				compose_live_log(running_live, running_slide);
			} else {
				compose_logs(NULL, 0);
			}
			fb_reply("OKAY", "");
		} else if (starts_with(cmd, "reboot") || starts_with(cmd, "oem reboot")) {
			fb_reply("OKAY", "");
			if (PLAT == &platform_annibale || kernel_resources_held || firmware_fault) {
				/* annibale matches ArmPsciResetSystemLib; on any platform a held or faulted
				 * state takes the same direct door before the stall and the callbacks. A
				 * return means no reset: the loop below stays usable and owns the cable. */
				int64_t status = psci_reset_attempt();
				print("PSCI SYSTEM_RESET returned %d; USB command loop retained\n", (uint64_t)status);
				set_result("reset-failed", (uint64_t)status, 0);
				compose_logs(NULL, 0);
			} else {
				BS->stall(200000);
				ST->rt->reset_system(0 /* EfiResetCold */, 0, 0, NULL);
			}
		} else {
			fb_reply("FAIL", "unknown command");
		}
	}
}

/*
 * A kernel is running on another core and this one watches it: see whether the host has said
 * something meanwhile, without waiting for it to. This is how a run can be asked to end, and
 * the screen and the log be had while it lasts.
 */
static void
usb_poll_command(void)
{
	uint32_t msg = UsbDeviceEventNoEvent;
	uint64_t size = 0, n;
	usb_device_event_data data;
	char cmd[128];

	if (USB == NULL) {
		return;         /* this run was not started over USB */
	}
	/* LC-POLL-BEGIN -- extracted verbatim by tools/test-loader-lifecycle.py: the queue rules
	 * around the ownership flags of the LC-USB block; a queued request ends only through the
	 * shared verified rule, so the wait and the poll cannot grow two sets of conditions. */
	if (usb_command_bytes > 0) {
		n = (uint64_t)usb_command_bytes;
		usb_command_bytes = 0;
		goto command_ready;
	}
	if (!usb_rx_queued && !usb_out_lost && !usb_out_owned && usb_connected &&
	    !EFI_ERROR(USB->send(USB_EP_OUT, 511, usb_rx_buf))) {
		usb_rx_queued = 1;
		usb_out_owned = 1;
		usb_out_buf = usb_rx_buf;   /* what this queued request submitted, kept for the check */
		usb_out_len = 511;
		usb_command_bytes = 0;
		usb_out_invalid = 0;        /* a new request starts with a fresh delivery state */
	}
	if (EFI_ERROR(USB->handle_event(&msg, &size, &data))) {
		return;         /* a failed call delivered no event to act on */
	}
	if (msg == UsbDeviceEventDeviceStateChange) {
		if (size >= sizeof(data.device_state)) {
			usb_connected = (data.device_state == UsbDeviceStateConnected);
			if (!usb_connected) {
				if (usb_command_bytes > 0) {
					usb_command_bytes = 0;
				}
				/* a disconnect marks the owned records undeliverable: a terminal that
				 * still arrives only releases them, its bytes reach no command */
				if (usb_out_owned) {
					usb_out_invalid = 1;
				}
				if (usb_in_owned) {
					usb_in_invalid = 1;
				}
			}
		}
		return;   /* the cable moved: the queue flag and the ownership records stay as they are */
	}
	if (msg != UsbDeviceEventTransferNotification) {
		return;
	}
	if (size < sizeof(data.transfer)) {
		usb_short_event_stops_all();   /* nothing readable names the request it ends */
		return;
	}
	if (data.transfer.endpoint_index == USB_EP_IN) {
		/* a reply's terminal seen here finishes or isolates its record; its bytes belong
		 * to a caller that already walked away and are not read from this loop */
		usb_notification(0, &data, &n);
		return;
	}
	if (data.transfer.endpoint_index != USB_EP_OUT) {
		return;
	}
	/* a CompleteOK on a downed link is recycled by the shared rule: only a live link may
	 * hand bytes to a command */
	if (usb_notification(1, &data, &n) != 1 || !usb_connected || n == 0) {
		return;         /* kept, failed, isolated, link down or ended empty: the buffer is not read */
	}
command_ready:
	if (n > sizeof(cmd) - 1) {
		n = sizeof(cmd) - 1;
	}
	memcpy(cmd, usb_rx_buf, (size_t)n);
	cmd[n] = 0;
	fb_command(cmd);
	/* LC-POLL-END */
}

static void
usb_command_loop(void)
{
	screen_quiet = 1;
	for (;;) {
		char cmd[128];
		int64_t n;

		if (!usb_connected || firmware_fault || usb_out_lost || usb_in_lost) {
			ramdisk_fail();
		}
		if (!usb_connected) {
			/* the link wait ends on a cable event: Connected returns so this loop submits
			 * the first OUT; a disconnect just keeps waiting for the cable to come up */
			usb_wait(0xff, 0);
			continue;
		}
		n = usb_rx(usb_rx_buf, 511, 1);
		if (n <= 0) {
			continue;
		}
		if (n > (int64_t)sizeof(cmd) - 1) {
			n = sizeof(cmd) - 1;
		}
		memcpy(cmd, usb_rx_buf, (size_t)n);
		cmd[n] = 0;
		fb_command(cmd);
	}
}

/* ------------------------------------------------------------------------ */

/*
 * QEMU's "ramfb" display shows whatever guest memory it is told to, in any size; the firmware
 * only offers a few small landscape modes of it. So we point it at memory of our own with the
 * shape of the iPhone's screen, through the fw_cfg file "etc/ramfb" (the firmware's own mode
 * is simply replaced). In QEMU the display is then exactly what iOS expects; on the real phone
 * the kernel's display driver scales that picture to the panel.
 */
#define PHONE_WIDTH     1125            /* the screen of the iPhone X, whose identity we carry */
#define PHONE_HEIGHT    2436
#define FW_CFG_BASE     0x09020000ULL   /* QEMU virt: data at +0, selector at +8, DMA address at +16 */
#define FW_CFG_FILE_DIR 0x19
#define FW_CFG_DMA_SELECT 0x08
#define FW_CFG_DMA_WRITE  0x10

static uint32_t
be32(uint32_t v)
{
	return __builtin_bswap32(v);
}

static void
fw_cfg_read(void *buf, uint32_t len)
{
	for (uint32_t i = 0; i < len; i++) {
		((uint8_t *)buf)[i] = *(volatile uint8_t *)FW_CFG_BASE;
	}
}

static void
ramfb_phone_screen(void)
{
	static struct {
		uint64_t addr;
		uint32_t fourcc, flags, width, height, stride;
	} __attribute__((packed)) cfg;
	static struct {
		uint32_t control, length;
		uint64_t address;
	} dma;
	struct {
		uint32_t size;
		uint16_t select, reserved;
		char name[56];
	} file;
	uint32_t count, select = 0;
	uint64_t fb = 0, bytes = (uint64_t)PHONE_WIDTH * 4 * PHONE_HEIGHT;

	*(volatile uint16_t *)(FW_CFG_BASE + 8) = __builtin_bswap16(FW_CFG_FILE_DIR);
	fw_cfg_read(&count, sizeof(count));
	count = be32(count);
	for (uint32_t i = 0; i < count && i < 256 && select == 0; i++) {
		fw_cfg_read(&file, sizeof(file));
		if (str_eq(file.name, "etc/ramfb")) {
			select = __builtin_bswap16(file.select);
		}
	}
	if (select == 0 || EFI_ERROR(BS->allocate_pages(AllocateAnyPages, EfiLoaderData, (bytes + 0xFFF) >> 12, &fb))) {
		return;
	}
	memset((void *)(uintptr_t)fb, 0, (size_t)bytes);
	cfg.addr = __builtin_bswap64(fb);
	cfg.fourcc = be32(0x34325258);          /* 'XR24': blue, green, red, unused */
	cfg.flags = 0;
	cfg.width = be32(PHONE_WIDTH);
	cfg.height = be32(PHONE_HEIGHT);
	cfg.stride = be32(PHONE_WIDTH * 4);
	dma.control = be32(select << 16 | FW_CFG_DMA_SELECT | FW_CFG_DMA_WRITE);
	dma.length = be32(sizeof(cfg));
	dma.address = __builtin_bswap64((uint64_t)(uintptr_t)&cfg);
	*(volatile uint64_t *)(FW_CFG_BASE + 16) = __builtin_bswap64((uint64_t)(uintptr_t)&dma);
	if (dma.control != 0) {
		print("ramfb: control %x after the request\n", (uint64_t)be32(dma.control));
		return;
	}
	SCREEN.base = fb;
	SCREEN.width = PHONE_WIDTH;
	SCREEN.height = PHONE_HEIGHT;
	SCREEN.stride = PHONE_WIDTH * 4;
}

efi_status EFIAPI
efi_main(efi_handle image, efi_system_table *st)
{
	static const efi_guid gop_guid = EFI_GOP_GUID;
	struct payload_info pi;

	ST = st;
	BS = st->bs;
	IMAGE = image;

	BS->set_watchdog_timer(0, 0, 0, NULL);
	print("== XNULOADER v3 ==\n");
	print("CurrentEL=%x MIDR=%x MMFR0=%x CNTFRQ=%d image=%x\n", READ_SYSREG(CurrentEL) >> 2, READ_SYSREG(MIDR_EL1),
	    READ_SYSREG(ID_AA64MMFR0_EL1), READ_SYSREG(CNTFRQ_EL0), (uint64_t)(uintptr_t)&__ImageBase);

	PLAT = &platform_qemu_virt;
	if (EFI_ERROR(BS->locate_protocol(&gop_guid, NULL, (void **)&GOP)) || GOP->mode == NULL ||
	    GOP->mode->info == NULL || GOP->mode->fb_base == 0) {
		GOP = NULL;                     /* no display (QEMU without -device ramfb) */
#ifdef DIRECT_PHONE_PLAT
		/*
		 * A phone build must select annibale even without GOP. The device tree reserves
		 * splash_region at 0xfc800000+0x2b00000, matching MemoryMapLib's Display_Reserved.
		 * The P11 mode is 1156x2510. BootArgs.video uses that buffer for kernel output;
		 * visible scanout still depends on the panel state left by LK.
		 */
		PLAT = &platform_annibale;
		SCREEN.base = platform_annibale.fb_base;
		SCREEN.width = 1156;              /* P11 mode from the device tree and Android DRM */
		SCREEN.height = 2510;
		SCREEN.stride = 1156 * 4;
		print("display assumed at %x: LK splash region\n", SCREEN.base);
#endif
	} else {
		SCREEN.base = GOP->mode->fb_base;
		SCREEN.width = GOP->mode->info->width;
		SCREEN.height = GOP->mode->info->height;
		SCREEN.stride = GOP->mode->info->pixels_per_scanline * 4;
		for (unsigned i = 0; i < sizeof(platforms) / sizeof(platforms[0]); i++)
			if (platforms[i]->fb_base == SCREEN.base)
				PLAT = platforms[i];
#ifdef DIRECT_PHONE_PLAT
		/* built for one phone: a framebuffer none of the known ones matches is ours */
		if (PLAT == &platform_qemu_virt)
			PLAT = &platform_annibale;
#else
		if (PLAT == &platform_qemu_virt)
			ramfb_phone_screen();
#endif
		print("display %d x %d at %x\n", (uint64_t)SCREEN.width, (uint64_t)SCREEN.height, SCREEN.base);
	}
	print("platform %s\n", PLAT->name);
	skip_ufs_arbitration = (PLAT == &platform_annibale); /* ramdisk kernel, no UFS handover (k90-port) */

	install_exception_hook();
	wdt_arm();

	int have_payload = parse_payload(&pi, 0);
	if (have_payload && pi.usb_mode && usb_start()) {
		flush_log();
		inspect_storage(); /* read-only since review F09; the writes live behind "oem probe" */
		usb_command_loop();
	}

	/* One-shot mode: run the kernel that came with the image, then reset. */
	if (have_payload) {
		run_kernel(0, 0);
	}
	reboot();
	return 0;
}
