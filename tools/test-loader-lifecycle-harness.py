"""Embedded C text for tools/test-loader-lifecycle.py.

PRELUDE is the stub firmware harness (fake clock, captured prints, counting psci/storage/USB
stubs) and SCENARIOS the scenario main() that runs the extracted production blocks against
them. The two strings are concatenated with the extracted loader.c blocks into one C file, so
they only compile as part of that file; this module is pure data, read verbatim by the driver
and loaded by path from it. Split out to keep each file under the 1000-line limit (AGENTS.md 2).
"""

PRELUDE = r"""#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
static jmp_buf jb_exit;   /* before the psci stub: reset escapes through it */
@C_PRELUDE@
/* ---- harness scaffolding: fake clock, captured prints, counted firmware stubs ---- */
static uint64_t clk_now, clk_freq = 1000000000ULL;
#define CNTVCT_EL0 0
#define CNTFRQ_EL0 1
static uint64_t clock_value(int which)
{
	if (which == CNTFRQ_EL0) { return clk_freq; }
	if (which == DAIF) { return fake_daif; }
	if (which == S3_0_C12_C12_7) { return fake_group1; }
	return clk_now;
}
#define READ_SYSREG(r) clock_value(r)
#define EFIAPI
@EXC@
static char out_cap[1 << 16];
static size_t out_len;
static int fail_count;
static int64_t print(const char *fmt, ...)
{
	va_list ap;
	int n;
	va_start(ap, fmt);
	n = vsnprintf(out_cap + out_len, sizeof(out_cap) - out_len, fmt, ap);
	va_end(ap);
	if (n > 0 && (out_len += (size_t)n) >= sizeof(out_cap)) {
		out_len = sizeof(out_cap) - 1;
	}
	return n;
}
static void put_char(char c) { if (out_len < sizeof(out_cap) - 1) { out_cap[out_len++] = c; } }
static int cap_has(const char *s) { return strstr(out_cap, s) != NULL; }
static void cap_reset(void) { out_cap[0] = 0; out_len = 0; }
#define CK(cond, name) do { if (!(cond)) { printf("FAIL %s\n", name); fail_count++; } } while (0)
#define CK0(name) do { printf("FAIL %s\n", name); fail_count++; } while (0)
#define START_SECONDS @START@
#define SILENCE_SECONDS @SILENCE@
#define CHECKPOINT_SELFTEST @SELFTEST@
#define KERNEL_LIVE_MAGIC2 @MAGIC2@
@LIVE@
/* platform and run_on_other_cpu context for the LC-ENTER bridge */
struct platform { int log_other_cpu; };
static const struct platform platform_annibale = { 1 };
static const struct platform plat_two = { 1 };
static const struct platform *PLAT;
static int screen_quiet;
static int firmware_fault;
static uint64_t mpidr = 0x81000000ULL, started, last_pet, last_battery, last_beat_at;
static int64_t status;
static char other_cpu_entry, other_cpu_start;
static void usb_poll_command(void);
/* psci: scripted answer, captured call, per-core affinity list (raw ids from the spec) */
#define PSCI_AFFINITY_INFO 0xC4000004u
static int psci_calls;
static uint64_t psci_fn, psci_a1, psci_a2, psci_a3;
@PSCI@
static void flush_dcache(uint64_t a, uint64_t b) { (void)a; (void)b; }
static void clean_dcache(uint64_t a, uint64_t b) { (void)a; (void)b; }
/* ---- stubs for LC-WRITE ---- */
typedef uint64_t efi_status;
#define EFI_ERROR(s) (((int64_t)(s)) < 0)
struct h_media { uint64_t block_size, io_align, media_id; };
struct h_bio {
	struct h_media *media;
	efi_status (*read_blocks)(struct h_bio *, uint64_t, uint64_t, uint64_t, void *);
	efi_status (*write_blocks)(struct h_bio *, uint64_t, uint64_t, uint64_t, void *);
	efi_status (*flush_blocks)(struct h_bio *);
};
typedef struct h_bio efi_block_io;
static char out_buf[8192], block_buffer[16384];
static uint32_t staged_len;
static int compose_calls, find_calls, w_blocks, r_blocks, flushes;
static int bio_present, uefi_list = 1, hc_log_skips, hc_known;
static struct h_media h_media = { 512, 0, 7 };
static efi_status h_write(efi_block_io *b, uint64_t m, uint64_t lba, uint64_t bytes, void *buf)
{ (void)b; (void)m; (void)lba; (void)buf; w_blocks += (int)(bytes / 512); return 0; }
static efi_status h_read(efi_block_io *b, uint64_t m, uint64_t lba, uint64_t bytes, void *buf)
{
	(void)m; (void)lba;
	if (bytes <= sizeof(block_buffer)) { memcpy(buf, out_buf, bytes); }
	r_blocks++;
	return 0;
}
static efi_status h_flush(efi_block_io *b) { (void)b; flushes++; return 0; }
static efi_block_io h_bio = { &h_media, h_read, h_write, h_flush };
static efi_block_io *find_log_partition(void) { find_calls++; seq('F'); return bio_present ? (efi_block_io *)&h_bio : NULL; }
static void compose_logs(const char *k, uint64_t l) { (void)k; (void)l; compose_calls++; staged_len = 100; seq('C'); }
static int hc_list_is_uefis(void) { return uefi_list; }
static void hc_print_state(const char *w) { (void)w; }
static int skip_ufs_arbitration = 1;
static void write_logs(const char *klog, uint64_t klog_len);
static void flush_log(void) { write_logs(NULL, 0); }
/* ---- stubs for LC-WATCH ---- */
static volatile uint64_t other_cpu_done, other_cpu_stage;
enum { CTX_LOG_PA = 0, CTX_KSP = 10 };
static uint64_t loader_ctx[16];
static uint64_t seconds_since(uint64_t then) { return (clk_now - then) / clk_freq; }
static int wdt_armed = 1, pets, charger_polls, block_polls, hung_snaps, charger_enabled;
static void wdt_pet(void) { pets++; seq('W'); }
static void charger_poll(void) { charger_polls++; }
static void block_poll(void) { block_polls++; }
static void log_of_hung_kernel(const struct kernel_live *l, uint64_t s) { (void)l; (void)s; hung_snaps++; }
/* ---- stubs for LC-RETURN ---- */
static const char *takebacks[8];
static int takebacks_n;
static void hc_take_back(const char *when) { if (takebacks_n < 8) { takebacks[takebacks_n++] = when; } seq('T'); }
static struct { struct { uint64_t switched_in, switched_out; } kernel_native; } block_shared;
static uint64_t held_daif, held_group1;
static int held_irq_masked;
static const char *recording_data;
static uint64_t recording_len, sound_len;
static const char *sound_data;
/* ---- stubs for LC-USB / LC-POLL / LC-EXC ---- */
#define USB_EP_OUT 0x01
#define USB_EP_IN  0x81
#define USB_CHUNK (1024 * 1024)   /* block-external, mirrored; the driver pins it below and the */
                                  /* wiring check pins the production #define to the same value */
static uint8_t rx_store[1024], tx_store[1024];
static uint8_t *usb_rx_buf = rx_store, *usb_tx_buf = tx_store;
/* The event/status/link enums and the event union arrive verbatim from the production
 * xnuloader/efi.h by the driver, review L10; the raw numbers below are written independently
 * from the ABL interface that header documents, so reordering either side fails here. */
@USB@
_Static_assert(UsbDeviceEventNoEvent == 0 && UsbDeviceEventDeviceStateChange == 1 && UsbDeviceEventTransferNotification == 2 && UsbDeviceEventOemEvent == 3, "usb event raw numbers");
_Static_assert(UsbDeviceStateConnected == 0 && UsbDeviceStateDisconnected == 1, "usb link state raw numbers");
_Static_assert(UsbDeviceTransferStatusActive == 0 && UsbDeviceTransferStatusCompleteOK == 1 && UsbDeviceTransferStatusCancelled == 2 && UsbDeviceTransferStatusCompleteError == 3, "usb transfer status raw numbers");
_Static_assert(sizeof(usb_device_event_data) == 64 && sizeof(((usb_device_event_data *)0)->transfer) == 24 && offsetof(usb_device_event_data, transfer.status) == 0 && offsetof(usb_device_event_data, transfer.endpoint_index) == 4 && offsetof(usb_device_event_data, transfer.bytes_completed) == 8 && offsetof(usb_device_event_data, transfer.data_buffer) == 16, "event union keeps raw[8] and the 24-byte transfer record");
@DESCSET@
struct h_usb {
	efi_status (*handle_event)(uint32_t *msg, uint64_t *size, usb_device_event_data *data);
	efi_status (*send)(uint8_t endpoint, uint64_t len, void *buf);
	efi_status (*stop)(void);
	efi_status (*abort_xfer)(uint8_t endpoint);
	efi_status (*set_endpoint_stall_state)(uint8_t endpoint, uint8_t state);
	efi_status (*start_ex)(usb_device_descriptor_set *set);
	efi_status (*allocate_transfer_buffer)(uint64_t size, void **buffer);
};
static struct h_usb *USB;
static usb_device_descriptor_set usb_desc_set;   /* the set the production usb_start() filled */
static int usb_connected = 1, block_enabled = 1;
static const struct kernel_live *running_live;
static int ev_calls, ev_sends, ev_after, ev_change, ev_msg, ev_change_state, ev_err, ev_status = -1;
static uint8_t ev_ep = USB_EP_IN;
static uint64_t ev_bytes = 4096, ev_step, ev_size, ev_no_events;
static int ev_size_set;   /* without it the call reports a full payload; with it even size 0 is explicit */
static usb_device_event_data ev_prefix;
static int ev_prefix_pending;   /* one event before the repeating event, for endpoint interleaving */
static int ev_cancel_first;   /* report Cancelled for this many calls, then ev_status */
static void *ev_out_buf, *ev_in_buf, *ev_buf;   /* ev_status -1 = CompleteOK, ev_buf 0 = the sent buffer */
static efi_status ev_handle(uint32_t *msg, uint64_t *size, usb_device_event_data *data)
{
	ev_calls++;
	clk_now += ev_step;
	if (ev_err) {
		return 1;   /* the call itself failed: a correct consumer must read none of the outputs */
	}
	if (ev_prefix_pending) {
		ev_prefix_pending = 0;
		*msg = UsbDeviceEventTransferNotification;
		*size = sizeof(*data);
		*data = ev_prefix;
		return 0;
	}
	/* the cap keeps a mutation from hanging: past it every event reports a link loss */
	if (ev_change || (ev_after && ev_calls > ev_after) || ev_calls > 400) {
		*msg = UsbDeviceEventDeviceStateChange;
		*size = sizeof(*data);
		data->device_state = (uint32_t)(ev_change ? ev_change_state : UsbDeviceStateDisconnected);
		return 0;
	}
	if (ev_no_events && ev_calls <= ev_no_events) {
		*msg = UsbDeviceEventNoEvent;
		*size = 0;
		return 0;
	}
	*msg = (uint32_t)ev_msg;
	*size = ev_size_set ? ev_size : sizeof(*data);
	if (ev_msg == UsbDeviceEventTransferNotification) {
		data->transfer.status = (ev_cancel_first && ev_calls <= ev_cancel_first)
		    ? UsbDeviceTransferStatusCancelled
		    : (ev_status < 0 ? UsbDeviceTransferStatusCompleteOK : (uint32_t)ev_status);
		data->transfer.endpoint_index = ev_ep;
		data->transfer.bytes_completed = ev_bytes;
		data->transfer.data_buffer = ev_buf ? ev_buf : (ev_ep == USB_EP_OUT ? ev_out_buf : ev_in_buf);
	}
	return 0;
}
static efi_status ev_send(uint8_t endpoint, uint64_t len, void *buf)
{
	ev_sends++;
	if (endpoint == USB_EP_OUT) { ev_out_buf = buf; } else { ev_in_buf = buf; }
	(void)len;
	return 0;
}
static int ab_calls, st_calls, sp_calls, se_calls, at_calls;
static efi_status ab_status, st_status, sp_status, se_status;
static efi_status ev_stop(void) { sp_calls++; return sp_status; }
static efi_status ev_abort(uint8_t endpoint) { ab_calls++; (void)endpoint; return ab_status; }
static efi_status ev_stall(uint8_t endpoint, uint8_t state) { st_calls++; (void)endpoint; (void)state; return st_status; }
static efi_status ev_start_ex(usb_device_descriptor_set *set) { se_calls++; (void)set; return se_status; }
static uint64_t at_size;
static efi_status ev_alloc(uint64_t size, void **buffer) { at_calls++; at_size = size; (void)buffer; return 0; }
static struct h_usb h_usb = { ev_handle, ev_send, ev_stop, ev_abort, ev_stall, ev_start_ex, ev_alloc };
static char fb_got[128];
static int fb_calls, fb_armed;
static void fb_command(const char *cmd)
{
	fb_calls++;
	strncpy(fb_got, cmd, sizeof(fb_got) - 1);
	if (fb_armed) { longjmp(jb_exit, 1); }
}
static char last_result[64];
static void set_result(const char *what, uint64_t a, uint64_t b) { snprintf(last_result, sizeof(last_result), "%s %d %x", what, (int)a, (unsigned)b); }
static char __ImageBase;
static int res_calls;
struct h_rt { efi_status (*reset_system)(int, unsigned, const void *, const void *); };
struct h_st { struct h_rt *rt; };
static efi_status h_reset(int a, unsigned b, const void *c, const void *d)
{ (void)a; (void)b; (void)c; (void)d; res_calls++; longjmp(jb_exit, 2); return 0; }
static struct h_rt h_rt = { h_reset };
static struct h_st h_st = { &h_rt };
#define ST (&h_st)

"""

SCENARIOS = r"""/* ---- scenarios ---- */
static const char *const blocked_cmds[] = { "oem ramdisk x", "download:1f000", "oem run", "oem selftest", "oem hang", "oem recording", "oem sound", "oem clearscreen", "oem memmap", "oem probe", "oem logwrite" };
static const char *const open_cmds[] = { "getvar:product", "getvar:result", "upload", "oem log", "oem screenshot", "reboot", "oem stop", "" };
static void sc_psci_spec(void)
{
	/* raw numbers from the SMC Calling Convention, written here without the loader macros */
	struct { int64_t got, want; const char *tag; } v[] = {
		{ PSCI_E_NOT_SUPPORTED, -1, "psci_spec_not_supported" }, { PSCI_E_INVALID_PARAMS, -2, "psci_spec_invalid_params" },
		{ PSCI_E_DENIED, -3, "psci_spec_denied" }, { PSCI_E_ALREADY_ON, -4, "psci_spec_already_on" },
		{ PSCI_E_ON_PENDING, -5, "psci_spec_on_pending" }, { PSCI_E_INTERNAL_FAILURE, -6, "psci_spec_internal_failure" },
		{ PSCI_E_NOT_PRESENT, -7, "psci_spec_not_present" }, { PSCI_E_DISABLED, -8, "psci_spec_disabled" },
		{ PSCI_E_INVALID_ADDRESS, -9, "psci_spec_invalid_address" } };
	for (unsigned i = 0; i < sizeof(v) / sizeof(v[0]); i++) {
		CK(v[i].got == v[i].want, v[i].tag);
	}
	CK(PSCI_CPU_ON == 0xC4000003u, "psci_spec_cpu_on_id");
	CK(PSCI_AFFINITY_OFF == 1, "psci_spec_affinity_off");
	CK(USB_EP_OUT == 0x01 && USB_EP_IN == 0x81, "spec_usb_eps");
}
static void one_issue(int64_t answer, int64_t want_held, const char *tag)
{
	kernel_resources_held = 0; psci_next = answer;
	int64_t got = lc_cpu_on_issue(0x81000000ULL, 0xdead0000ULL, 0xbeef0000ULL);
	CK(got == answer, tag);
	CK(kernel_resources_held == want_held, tag);
}
static void sc_cpu_on(void)
{
	one_issue(0, 1, "onissue_ok"); one_issue(-1, 0, "onissue_neg1"); one_issue(-2, 0, "onissue_neg2");
	one_issue(-3, 0, "onissue_neg3"); one_issue(-9, 0, "onissue_neg9");
	one_issue(-4, 1, "onissue_neg4_already_on"); one_issue(-5, 1, "onissue_neg5_on_pending");
	one_issue(-6, 1, "onissue_neg6_internal"); one_issue(-7, 1, "onissue_neg7_not_present");
	one_issue(-8, 1, "onissue_neg8_disabled"); one_issue(-99, 1, "onissue_neg99_unknown"); one_issue(7, 1, "onissue_pos");
	kernel_resources_held = 0;
	psci_next = 0;
	lc_cpu_on_issue(0x81000000ULL, 0x1000ULL, 0x2000ULL);
	CK(psci_fn == 0xC4000003u, "onissue_fn");
	CK(psci_a1 == 0x81000000ULL && psci_a2 == 0x1000ULL && psci_a3 == 0x2000ULL, "onissue_args");
	one_issue(-2, 0, "onissue_refused_reopens");
	CK(!lc_cmd_denied(kernel_resources_held, 0, 0, "download:1f000"), "onissue_refused_download_open");
	CK(!lc_cmd_denied(kernel_resources_held, 0, 0, "oem stop"), "onissue_refused_stop_open");
	one_issue(-4, 1, "onissue_already_on_stays");
	CK(lc_cmd_denied(kernel_resources_held, 0, 0, "download:1f000"), "onissue_already_on_download_denied");
	CK(lc_cmd_denied(kernel_resources_held, 0, 0, "oem run"), "onissue_already_on_run_denied");
}
static void sc_commands(void)
{
	for (unsigned i = 0; i < sizeof(blocked_cmds) / sizeof(blocked_cmds[0]); i++) {
		CK(lc_cmd_denied(1, 0, 0, blocked_cmds[i]), "held_block_cmd");
		CK(!lc_cmd_denied(0, 0, 0, blocked_cmds[i]), "free_open_cmd");
	}
	for (unsigned i = 0; i < sizeof(open_cmds) / sizeof(open_cmds[0]); i++) {
		CK(!lc_cmd_denied(1, 0, 0, open_cmds[i]), "held_open_cmd");
	}
	CK(lc_cmd_denied(1, 0, 1, "download:1f000"), "live_block_cmd");
	CK(lc_cmd_denied(0, 1, 0, "download:1f000"), "fault_block_cmd");
	CK(lc_busy(1, 0, 0), "busy_held");
	CK(lc_busy(0, 0, 1), "busy_live");
	CK(!lc_busy(0, 0, 0), "busy_free");
	CK(lc_stop_action(1, 0) == LC_STOP_HELD, "stop_held_honest");
	CK(lc_stop_action(0, 1) == LC_STOP_NOTIFY, "stop_live_notifies");
	CK(lc_stop_action(0, 0) == LC_STOP_IDLE, "stop_idle_okay");
	kernel_resources_held = 1;
	clk_now = 12345;
	lc_cmd_denied(kernel_resources_held, 0, 0, "oem run");
	CK(kernel_resources_held == 1 && clk_now == 12345, "decide_pure");
}
/* the real bridge: request, exemption window, the log_other_cpu flush, before the loop */
static void sc_enter(void)
{
	PLAT = &platform_annibale;   /* log_other_cpu = 1, the reachable annibale case */
	psci_next = -4;
	kernel_resources_held = active_watch = 0;
	bio_present = 1;
	find_calls = w_blocks = compose_calls = 0; cap_reset();
	run_enter();
	CK(status == -4, "bridge_refused_status");
	CK(kernel_resources_held == 1, "bridge_refused_holds");
	CK(active_watch == 0, "bridge_refused_no_exemption");
	CK(find_calls == 0 && w_blocks == 0, "bridge_refused_no_storage");
	CK(compose_calls == 1 && cap_has("the firmware answers") && !cap_has("KERNEL LOG"), "bridge_refused_stages_only");
	psci_next = kernel_resources_held = 0;
	find_calls = w_blocks = 0; cap_reset();
	run_enter();
	CK(status == 0 && active_watch == 1, "bridge_ok_watch_opens");
	CK(find_calls == 1 && w_blocks > 0, "bridge_ok_flush_writes");
	active_watch = 0;
}
static uint64_t last_pet_v, last_batt_v, last_beat_v, last_beat_at_v;
static int arrived_v;
static int64_t status_v;
static int wr(struct kernel_live *l, uint64_t began)
{
	return lc_watch_round(l, 0, began, 60, &arrived_v, &last_pet_v, &last_batt_v, &last_beat_v, &last_beat_at_v, &status_v);
}
static void watch_reset(uint64_t t)
{
	other_cpu_done = 0;
	other_cpu_stage = arrived_v = 1;
	last_pet_v = last_batt_v = last_beat_v = last_beat_at_v = clk_now = t;
	status_v = pets = charger_polls = block_polls = hung_snaps = 0;
	ev_calls = ev_sends = ev_after = ev_change = ev_change_state = 0;
	ev_msg = UsbDeviceEventNoEvent;
	ev_step = 0; USB = &h_usb;
	usb_connected = 1;
}
static void sc_watch(void)
{
	static struct kernel_live live;
	live.heartbeat = 100;
	kernel_resources_held = 1;   /* as after an accepted CPU_ON */
	watch_reset(1000ULL * clk_freq);
	for (int r = 0; r < 3; r++) {
		live.heartbeat += 1;
		clk_now += 2 * clk_freq;
		CK(wr(&live, 1000ULL * clk_freq) == 0, "watch_normal_round");
	}
	CK(block_polls >= 3 && status_v == 0, "watch_normal_services");
	watch_reset(2000ULL * clk_freq);
	other_cpu_done = 1;
	CK(wr(&live, 2000ULL * clk_freq) == 1, "watch_done_stops");
	CK(status_v == 0 && block_polls == 0 && charger_polls == 0 && ev_calls == 0, "watch_done_no_services");
	watch_reset(3000ULL * clk_freq);
	other_cpu_stage = 0;
	arrived_v = 0;
	charger_enabled = 1;
	clk_now += (START_SECONDS + 1) * clk_freq;
	CK(wr(&live, 3000ULL * clk_freq) == 1, "watch_notstarted_stops");
	CK(status_v == OTHER_CPU_NOT_STARTED, "watch_notstarted_status");
	CK(block_polls == 0 && charger_polls == 0 && ev_calls == 0 && pets == 0, "watch_notstarted_no_services");
	charger_enabled = 0;
	watch_reset(4000ULL * clk_freq);
	CK(wr(&live, 4000ULL * clk_freq) == 0, "watch_hung_first_round");
	block_polls = charger_polls = ev_calls = 0;
	/* the heartbeat stays where the first round recorded it: the kernel said nothing more */
	clk_now += (SILENCE_SECONDS + 1) * clk_freq;
	CK(wr(&live, 4000ULL * clk_freq) == 1, "watch_hung_stops");
	CK(status_v == OTHER_CPU_HUNG && hung_snaps == 1, "watch_hung_status_and_snapshot");
	CK(block_polls == 0 && charger_polls == 0 && ev_calls == 0, "watch_hung_no_services_after");
	watch_reset(5000ULL * clk_freq);
	charger_enabled = 1;
	uefi_list = 0;
	clk_now += 6 * clk_freq;
	live.heartbeat += 1;
	wr(&live, 5000ULL * clk_freq);
	CK(charger_polls == 0, "watch_charger_waits_for_uefi_list");
	uefi_list = 1;
	clk_now += 6 * clk_freq;
	live.heartbeat += 1;
	wr(&live, 5000ULL * clk_freq);
	CK(charger_polls == 1, "watch_charger_polls_for_uefi_list");
	charger_enabled = 0;
	kernel_resources_held = 0;
	usb_rx_queued = usb_out_owned = usb_in_owned = usb_out_lost = usb_in_lost = usb_out_invalid = usb_in_invalid = 0;
}
static void ret_reset(int64_t *states)
{
	aff_knobs();
	for (int i = 0; i < 8; i++) {
		psci_aff_ans[i] = states[i];
	}
	psci_calls = 0;
	takebacks_n = held_irq_masked = 0;
}
static void sc_return(void)
{
	int64_t all_off[8] = { 1, 1, 1, 1, 1, 1, 1, 1 };
	static const int64_t bads[] = { 0, 2, -1, 5 };
	kernel_resources_held = 1;
	ret_reset(all_off);
	lc_after_return(1, cpus, 3);
	CK(kernel_resources_held == 0, "ret_other_off_releases");
	CK(takebacks_n == 1 && takebacks[0] && !strcmp(takebacks[0], "the kernel came back"), "ret_other_off_takeback");
	for (unsigned b = 0; b < 4; b++) {
		int64_t st[8] = { 1, 1, 1, 1, 1, 1, 1, 1 };
		st[2] = bads[b];
		kernel_resources_held = 1;
		cap_reset();
		ret_reset(st);
		lc_after_return(1, cpus, 3);
		CK(kernel_resources_held == 1, "ret_other_notoff_holds");
		CK(takebacks_n == 0, "ret_other_notoff_no_takeback");
		CK(psci_calls == (b < 2 ? 4 : 3), "ret_bad_calls");
		CK(cap_has("the firmware answers"), "ret_prints_state");
	}
	{
		int64_t st[8] = { 1, 1, -1, 1, 1, 1, 1, 1 };
		cap_reset();
		kernel_resources_held = 1;
		ret_reset(st);
		lc_after_return(1, cpus, 3);
		CK(cap_has("answers -1"), "ret_prints_neg1");
	}
	kernel_resources_held = 0;
	ret_reset(all_off);
	lc_after_return(0, cpus, 3);
	CK(kernel_resources_held == 0 && psci_calls == 2, "ret_same_off_free");
	CK(takebacks_n == 1, "ret_same_off_takeback");
	for (unsigned b = 0; b < 4; b++) {
		int64_t st[8] = { 1, 1, 1, 1, 1, 1, 1, 1 };
		st[1] = bads[b];
		kernel_resources_held = 0;
		ret_reset(st);
		lc_after_return(0, cpus, 3);
		CK(kernel_resources_held == 1 && psci_calls == (b < 2 ? 4 : 1), "same_notoff_holds");
		CK(takebacks_n == 0, "same_notoff_no_takeback");
		CK(lc_cmd_denied(kernel_resources_held, 0, 0, "download:1f000"), "same_notoff_download_denied");
	}
}
static void sc_collect(void)
{
	static uint64_t rec[2], snd[2], junk[2];
	static struct kernel_live live;
	rec[0] = (uint64_t)(uintptr_t)"screen data"; rec[1] = 1000;
	snd[0] = (uint64_t)(uintptr_t)"sound data"; snd[1] = 900; memset(&live, 0, sizeof(live));
	live.magic2 = KERNEL_LIVE_MAGIC2; live.recording = (uint64_t)(uintptr_t)junk - 0x100000;
	live.sound = (uint64_t)(uintptr_t)snd - 0x100000;
	kernel_resources_held = 1;
	lc_after_return_collect(&live, 0x100000, 0);
	CK(recording_data == NULL && sound_data == NULL, "collect_held_skips_kernel_memory");
	kernel_resources_held = 0;
	junk[0] = 0; junk[1] = 9;
	lc_after_return_collect(&live, 0x100000, 0);
	CK(recording_data == NULL, "collect_bounds_header");
	junk[0] = (uint64_t)(uintptr_t)rec;
	junk[1] = 1000;
	lc_after_return_collect(&live, 0x100000, 0);
	CK(recording_data != NULL && recording_len == 1000, "collect_reads_when_free");
	lc_after_return_collect(NULL, 0, 0);
	CK(recording_data == NULL && sound_data == NULL, "collect_no_live");
}
static void sc_write_logs(void)
{
	kernel_resources_held = 1;
	active_watch = firmware_fault = 0;
	bio_present = 1;
	find_calls = w_blocks = flushes = compose_calls = 0;
	cap_reset();
	write_logs("k", 1);
	CK(compose_calls == 1, "wlog_held_stages");
	CK(find_calls == 0 && w_blocks == 0 && flushes == 0, "wlog_held_no_firmware_calls");
	CK(cap_has("KERNEL LOG"), "wlog_held_echoes");
	active_watch = 1;   /* the watch loop keeps its cooperative flush (redwood notes) */
	find_calls = w_blocks = 0;
	write_logs("k", 1);
	CK(find_calls == 1 && w_blocks > 0, "wlog_watch_allowed");
	active_watch = 0;
	kernel_resources_held = 0;
	find_calls = w_blocks = flushes = 0;
	cap_reset();
	write_logs("k", 1);
	CK(find_calls == 1 && w_blocks > 0 && flushes == 1, "wlog_free_writes");
	CK(cap_has("read back the same"), "wlog_free_readback");
	bio_present = 0;
	find_calls = 0;
	write_logs("k", 1);
	CK(find_calls == 1 && cap_has("END OF LOG"), "wlog_nobio_echo");
	kernel_resources_held = 1; psci_next = 0;
	psci_call(0x84000009u, 0, 0, 0);   /* the reset call does not clear the hold on its own */
	CK(kernel_resources_held == 1, "reset_keeps_hold");
	find_calls = w_blocks = 0;
	write_logs("k", 1);
	CK(find_calls == 0 && w_blocks == 0, "reset_hold_still_gated");
	kernel_resources_held = 0;
}
static void usb_clean(void) {
	USB = &h_usb; usb_connected = kernel_resources_held = 1;
	firmware_fault = 0; running_live = NULL;
	usb_rx_queued = usb_out_owned = usb_in_owned = usb_out_lost = usb_in_lost = usb_out_invalid = usb_in_invalid = 0;
	usb_in_since = 0; usb_in_recoveries = 0; usb_in_abort_requested = 0; usb_command_bytes = -1;
	ev_calls = ev_sends = ev_after = ev_change = ev_change_state = 0;
	ev_msg = UsbDeviceEventNoEvent; ev_step = clk_freq / 10; clk_now = 0;
	ev_err = 0; ev_status = -1; ev_size = 0; ev_size_set = 0; ev_no_events = 0; ev_buf = NULL;
	ev_cancel_first = ev_prefix_pending = 0;
	ev_out_buf = ev_in_buf = NULL; usb_out_buf = NULL; usb_out_len = 0; usb_in_buf = NULL; usb_in_len = 0;
	ab_calls = st_calls = sp_calls = se_calls = at_calls = 0; at_size = 0;
	ab_status = st_status = sp_status = se_status = 0;
	h_usb.stop = ev_stop; h_usb.start_ex = ev_start_ex; h_usb.abort_xfer = ev_abort;
	h_usb.set_endpoint_stall_state = ev_stall; h_usb.allocate_transfer_buffer = ev_alloc;
}
static void sc_usb(void)
{
	usb_clean();
	int64_t r = usb_wait(USB_EP_IN, 1);
	CK(r == -1, "usb_held_bounded_exit");
	CK(ev_calls <= 60 && ev_calls > 20, "usb_held_bounded_time");
	CK(pets >= 2, "usb_held_feeds_watchdog");
	/* nothing held and no fault: the wait still ends, on the longer idle budget, and the link
	 * stays up because no cable event ended it (the old rule waited until one did) */
	kernel_resources_held = 0;
	ev_calls = 0; ev_step = clk_freq / 20;
	r = usb_wait(USB_EP_OUT, 1);
	CK(r == -1 && ev_calls > 150 && ev_calls < 260 && usb_connected == 1, "usb_idle_bounded_exit");
	/* a verified completion for the submitted request answers with its length; a
	 * notification for another endpoint does not end the wait */
	ev_calls = ev_after = 0;
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_IN; ev_bytes = 512;
	r = usb_tx((const uint8_t *)"AB", 512);
	CK(r == 512, "usb_transfer_complete");
	ev_ep = USB_EP_OUT; ev_after = 7; ev_calls = 0;
	r = usb_tx((const uint8_t *)"CD", 512);
	CK(r == -1 && ev_calls == 8 && usb_in_owned == 1 && usb_in_lost == 0, "usb_other_endpoint_ignored");
}
static void sc_usb_owner(void)
{
	/* a held IN transfer that times out keeps ownership: no resend, no write over the busy
	 * buffer, and the terminal that still arrives ends the reply */
	usb_clean();
	int64_t r = (memcpy(tx_store, "AA", 2), usb_tx((const uint8_t *)"AB", 2));
	CK(r == -1 && usb_in_owned == 1 && usb_in_lost == 0, "usb_tx_timeout_keeps");
	usb_in_invalid = 0;  /* a live transfer: the veto applies before the abort age */
	usb_in_since = clk_now;
	tx_store[0] = 'Z';   /* sentinel: the owned buffer must not be overwritten while owned */
	r = usb_tx((const uint8_t *)"CD", 2);
	CK(r == -1 && ev_sends == 1 && tx_store[0] == 'Z', "usb_tx_owned_no_resend_no_overwrite");
	/* Cancelled ends the old reply as a failure: the record clears and the next send goes */
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_IN; ev_status = UsbDeviceTransferStatusCancelled;
	ev_size_set = 0; ev_bytes = 0; poll_once();
	CK(usb_in_owned == 0 && usb_in_lost == 0, "usb_cancel_frees_in");
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_IN; ev_bytes = 2; ev_status = -1;
	ev_sends = 0;
	r = usb_tx((const uint8_t *)"EF", 2);
	CK(r == 2 && ev_sends == 1 && tx_store[0] == 'E', "usb_tx_after_cancel");
	/* a CompleteOK no wait is left to take: the record clears, the bytes go unread */
	usb_clean();
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_IN; ev_status = UsbDeviceTransferStatusActive;
	(void)usb_tx((const uint8_t *)"OKAYlate", 8);
	ev_status = -1; ev_bytes = 8;
	r = usb_wait(USB_EP_IN, 0);
	CK(r == -1 && usb_in_owned == 0 && usb_in_lost == 0, "usb_late_ok_recycled");
	/* an OUT receive that times out hands the buffer back to the queue rule */
	usb_clean();
	r = usb_rx(rx_store, 64, 0);
	CK(r == -1 && usb_out_lost == 0 && usb_rx_queued == 0 && usb_out_owned == 1 && usb_out_invalid == 1, "usb_rx_timeout_marks_record");
	ev_sends = 0; ev_change = 1; ev_change_state = UsbDeviceStateConnected; poll_once();
	CK(ev_sends == 0 && usb_out_owned == 1, "usb_poll_owned_no_resend");
	/* the late OK of a timed-out request only releases the record: its bytes reach no command */
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 2; ev_change = 0; strcpy((char *)rx_store, "y"); fb_calls = 0; poll_once();
	CK(fb_calls == 0 && usb_out_owned == 0 && usb_out_invalid == 0 && usb_rx_queued == 0, "usb_timeout_old_ok_not_delivered");
	/* a disconnect ends the wait and keeps the queued request: downed link dispatches nothing,
	 * a CompleteOK arriving on it is recycled and the poll resends nothing */
	ev_msg = UsbDeviceEventNoEvent; poll_once();   /* the receive for the next command is queued */
	ev_change = 1; ev_change_state = UsbDeviceStateDisconnected; ev_calls = ev_sends = 0;
	r = usb_wait(USB_EP_OUT, 1);
	CK(r == -1 && ev_calls == 1 && usb_out_lost == 0 && usb_rx_queued == 1 && usb_out_owned == 1 && ev_sends == 0, "usb_disconnect_keeps_request");
	ev_change = 0;
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 2; strcpy((char *)rx_store, "z"); fb_calls = 0; poll_once();
	CK(fb_calls == 0 && usb_out_owned == 0 && usb_rx_queued == 0, "usb_late_ok_downlink_recycled");
	/* the other endpoint's terminal ends that request and leaves the queued receive alone */
	usb_clean(); poll_once();
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_IN; ev_status = UsbDeviceTransferStatusCancelled;
	poll_once();
	CK(usb_out_owned == 1 && usb_rx_queued == 1 && usb_in_lost == 0, "usb_other_endpoint_not_confirmed");
	/* a structurally isolated OUT channel keeps its buffer across a reconnect, refuses new
	 * sends and resends nothing: external reset is the recovery boundary */
	usb_clean(); poll_once();
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 2;
	ev_buf = tx_store; fb_calls = 0; poll_once();
	CK(usb_out_lost == 1 && usb_out_owned == 1, "usb_isolate_wrong_buffer");
	ev_msg = UsbDeviceEventNoEvent; ev_change = 1; ev_change_state = UsbDeviceStateConnected;
	CK(usb_wait(USB_EP_OUT, 1) == -1 && usb_connected == 1 && usb_out_lost == 1 && usb_out_owned == 1
	    && ev_calls <= 60, "usb_relink_still_lost");
	ev_change = 0; ev_sends = 0; poll_once();
	CK(ev_sends == 0, "usb_poll_lost_no_resend");
	/* isolation through the command wait instead of the queued poll: the handoff takes the
	 * queue flag, so only the isolation itself can refuse the poll's next receive */
	usb_clean(); poll_once();
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_calls = ev_sends = 0;
	ev_buf = tx_store; ev_bytes = 2;
	CK(usb_rx(usb_rx_buf, 511, 0) == -1 && usb_out_lost == 1 && usb_rx_queued == 0, "usb_wait_isolate_unqueued");
	ev_sends = 0; poll_once();
	CK(ev_sends == 0 && usb_out_lost == 1, "usb_poll_lost_send_refused");
}
/* a reply the host walked away from: the wait expires in the held state and the record stays
 * owned and marked, which is the state the sampler met on the phone */
static void usb_wedge_in(void)
{
	usb_clean();
	kernel_resources_held = 1;
	ev_step = clk_freq / 10;
	memcpy(tx_store, "AB", 2);
	CK(usb_tx((const uint8_t *)"AB", 2) == -1 && usb_in_owned == 1 && usb_in_invalid == 1,
	   "recover_setup_wedged");
}
static void sc_usb_recover(void)
{
	/* An abort request cannot release or overwrite the submitted buffer, even on success. */
	usb_wedge_in();
	ev_calls = ev_sends = 0;
	CK(usb_tx((const uint8_t *)"OKAY", 4) == -1, "recover_abort_keeps_reply_pending");
	CK(ab_calls == 1 && usb_in_owned && usb_in_invalid && usb_in_abort_requested,
	   "recover_abort_requested_once");
	CK(ev_calls == 0 && ev_sends == 0 && !memcmp(tx_store, "AB", 2),
	   "recover_abort_consumes_no_events_or_bytes");
	CK(usb_tx((const uint8_t *)"OKAY", 4) == -1 && ab_calls == 1,
	   "recover_repoll_no_repeat_abort");
	CK(st_calls == 0 && sp_calls == 0 && se_calls == 0 && at_calls == 0,
	   "recover_no_halt_restart_or_allocation");

	/* OUT can carry reboot while IN still belongs to the controller. The recovery helper
	 * must leave the event for usb_wait and keep the receive buffer's identity unchanged. */
	memcpy(rx_store, "reboot", 6);
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 6;
	CK(usb_rx(rx_store, 64, 0) == 6 && !memcmp(usb_rx_buf, "reboot", 6),
	   "recover_out_command_delivered");
	CK(usb_in_owned && usb_out_owned == 0 && usb_rx_buf == rx_store && usb_tx_buf == tx_store,
	   "recover_out_leaves_in_owned_and_buffers_stable");

	/* A normal completion or cancellation is handled by the existing verified rule. */
	ev_ep = USB_EP_IN; ev_bytes = 2; ev_status = UsbDeviceTransferStatusCancelled;
	CK(usb_wait(USB_EP_IN, 0) == -1 && !usb_in_owned && !usb_in_invalid,
	   "recover_verified_cancel_releases");
	ev_bytes = 4; ev_status = UsbDeviceTransferStatusCompleteOK;
	CK(usb_tx((const uint8_t *)"OKAY", 4) == 4 && !usb_in_abort_requested,
	   "recover_new_reply_resets_abort_mark");
	usb_wedge_in();
	(void)usb_abort_stale_in("test");
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_IN; ev_bytes = 2;
	CK(usb_wait(USB_EP_IN, 0) == -1 && !usb_in_owned,
	   "recover_verified_complete_releases_without_delivery");

	/* An IN terminal can precede the OUT completion in the same receive wait. */
	for (int bad = 0; bad < 3; bad++) {
		usb_wedge_in();
		ev_prefix_pending = 1;
		ev_prefix.transfer.endpoint_index = USB_EP_IN;
		ev_prefix.transfer.status = UsbDeviceTransferStatusCancelled;
		ev_prefix.transfer.data_buffer = bad == 1 ? rx_store : tx_store;
		ev_prefix.transfer.bytes_completed = bad == 2 ? 3 : 2;
		ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 6;
		CK(usb_rx(rx_store, 64, 0) == 6 && usb_in_owned == (bad != 0) && usb_in_lost == (bad != 0),
		   "recover_interleaved_in_terminal_then_out");
	}

	/* Abort support and success are optional: OUT and its bounded wait remain usable. */
	for (int no_abort = 0; no_abort < 2; no_abort++) {
		usb_wedge_in();
		if (no_abort) { h_usb.abort_xfer = NULL; } else { ab_status = 0x8000000000000001ULL; }
		ev_calls = 0; ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 6;
		CK(usb_rx(rx_store, 64, 0) == 6 && usb_in_owned && ab_calls == !no_abort,
		   "recover_abort_unavailable_out_still_received");
		ev_msg = UsbDeviceEventNoEvent; ev_calls = 0;
		CK(usb_rx(rx_store, 64, 0) == -1 && ev_calls > 20 && ev_calls < 60,
		   "recover_no_terminal_wait_still_bounded");
	}

	/* Malformed abort notifications use the same buffer and length checks as any terminal. */
	for (int overlong = 0; overlong < 2; overlong++) {
		usb_wedge_in();
		(void)usb_abort_stale_in("test");
		ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_IN;
		ev_status = UsbDeviceTransferStatusCancelled; ev_bytes = overlong ? 3 : 2;
		ev_buf = overlong ? NULL : rx_store;
		CK(usb_wait(USB_EP_IN, 0) == -1 && usb_in_owned && usb_in_lost,
		   "recover_bad_cancel_keeps_buffer_isolated");
		ev_buf = NULL; ev_ep = USB_EP_OUT; ev_bytes = 6; ev_status = UsbDeviceTransferStatusCompleteOK;
		CK(usb_rx(rx_store, 64, 0) == 6 && usb_in_owned,
		   "recover_isolated_in_keeps_out_usable");
	}

	usb_clean(); kernel_resources_held = 1; ev_step = clk_freq / 10;
	CK(usb_tx((const uint8_t *)"AB", 2) == -1, "recover_young_setup");
	usb_in_invalid = 0;
	usb_in_since = clk_now;
	ev_calls = 0;
	CK(usb_tx((const uint8_t *)"CD", 2) == -1 && ab_calls == 0,
	   "recover_young_kept");

	usb_wedge_in(); usb_in_invalid = 0;
	clk_now += (USB_IN_STALE_SECONDS + 1) * clk_freq;
	CK(usb_abort_stale_in("test") == -1 && ab_calls == 1 && usb_in_owned,
	   "recover_by_age_only_requests_abort");

	usb_wedge_in(); usb_in_recoveries = USB_IN_RECOVER_LIMIT;
	CK(usb_abort_stale_in("test") == -1 && ab_calls == 0 && usb_in_owned,
	   "recover_limit_refuses");
	usb_wedge_in(); usb_in_lost = 1;
	CK(usb_abort_stale_in("test") == -1 && ab_calls == 0 && usb_in_owned,
	   "recover_skips_isolated");
}
/* Command waits are polls of one receive, whereas a timed-out data receive is abandoned. */
static void sc_usb_idle_command(void)
{
	const char *commands[] = { "getvar:result", "reboot" };
	for (int held = 0; held < 2; held++) {
		for (unsigned c = 0; c < sizeof(commands) / sizeof(commands[0]); c++) {
			usb_clean(); kernel_resources_held = held;
			for (int round = 0; round < 4; round++) {
				ev_calls = 0;
				CK(usb_rx(rx_store, 511, 1) == -1 && usb_out_owned && !usb_out_invalid,
				   "idle_command_wait_preserves_delivery");
			}
			CK(clk_now > 10 * clk_freq && ev_sends == 1, "idle_command_held_over_ten_seconds");
			uint64_t len = strlen(commands[c]);
			memcpy(rx_store, commands[c], len);
			ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = len;
			CK(usb_rx(rx_store, 511, 1) == (int64_t)len && !memcmp(rx_store, commands[c], len)
			    && !usb_out_owned && ev_sends == 1, "idle_first_command_delivered");
		}
	}
	/* The mode, rather than a buffer size heuristic, determines late-byte delivery. */
	usb_clean();
	CK(usb_rx(rx_store, 511, 0) == -1 && usb_out_invalid, "idle_data_timeout_stays_invalid");
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 6;
	CK(usb_rx(rx_store, 511, 1) == -1 && !usb_out_owned && !usb_out_invalid,
	   "idle_abandoned_data_not_delivered_as_command");
}
static void sc_fault(void)
{
	kernel_resources_held = active_watch = 0; firmware_fault = 1;
	bio_present = 1;
	find_calls = w_blocks = 0; cap_reset();
	write_logs("k", 1);
	CK(find_calls == 0 && w_blocks == 0 && cap_has("KERNEL LOG"), "wlog_fault_echo_only");
	usb_clean(); kernel_resources_held = 0; firmware_fault = 1;
	CK(usb_wait(USB_EP_OUT, 1) == -1 && ev_calls <= 60, "usb_fault_bounded");
	firmware_fault = 0;
}
static void sc_repeat(void)
{
	for (int i = 0; i < 1000; i++) {
		one_issue(-4, 1, "repeat_stable");
		if (!lc_cmd_denied(kernel_resources_held, 0, 0, "oem run") || lc_stop_action(kernel_resources_held, 0) != LC_STOP_HELD) {
			CK0("repeat_stable");
			break;
		}
	}
}
/* fb_reply packs into its own stack buffer: usb_tx stays the only writer of the shared one */
static void sc_fb(void)
{
	static char long_text[201];

	usb_clean();
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_IN; ev_bytes = 8;
	fb_reply("OKAY", "base");
	CK(!usb_in_owned && !usb_in_lost && ev_sends == 1, "fb_healthy_first");
	CK(memcmp(tx_store, "OKAYbase", 8) == 0, "fb_healthy_first_bytes");
	fb_reply("OKAY", "next");
	CK(!usb_in_owned && !usb_in_lost && ev_sends == 2, "fb_healthy_second");
	CK(memcmp(tx_store, "OKAYnext", 8) == 0, "fb_healthy_second_bytes");
	/* the counterexample: the sentinel bytes stand before the IN transfer is submitted; after
	 * the timeout left the transfer owned, neither reply form may write over them */
	usb_clean();
	memset(tx_store, 0xa5, sizeof(tx_store));
	fb_reply("OKAY", "base");   /* one send, no confirmation: owned from here on */
	CK(usb_in_owned == 1 && usb_in_lost == 0 && ev_sends == 1, "fb_first_timeout");
	usb_in_invalid = 0;         /* a live transfer: neither reply form may write over it */
	usb_in_since = clk_now;
	uint8_t saved[128];
	memcpy(saved, tx_store, sizeof(saved));
	fb_reply_data(0x1234);
	CK(memcmp(saved, tx_store, sizeof(saved)) == 0 && ev_sends == 1, "fb_lost_data_preserves");
	fb_reply("FAIL", "next");
	CK(memcmp(saved, tx_store, sizeof(saved)) == 0, "fb_lost_reply_preserves");
	CK(ev_sends == 1, "fb_lost_no_resend");
	/* A verified terminal must end the old request before a new reply can reuse its buffer. */
	usb_in_invalid = 1;
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_IN; ev_bytes = 8;
	CK(usb_wait(USB_EP_IN, 0) == -1 && !usb_in_owned, "fb_terminal_releases_old_reply");
	ev_bytes = 5;
	CK(usb_tx((const uint8_t *)"again", 5) == 5 && ev_sends == 2, "fb_after_terminal_reuses");
	/* hard limit: a long text never packs past the local buffer (code 4 + text up to 63) */
	usb_clean();
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_IN; ev_bytes = 63;
	memset(long_text, 't', sizeof(long_text) - 1);
	memset(tx_store, 0xa5, 128);
	fb_reply("OKAY", long_text);
	CK(ev_sends == 1 && tx_store[0] == 'O' && tx_store[62] == 't' && tx_store[63] == 0xa5, "fb_hard_cap");
}

/* notification verification: only a CompleteOK matching the recorded request ends one; Active,
 * unknown values, wrong buffers, oversized counts, short events and failed calls behave as the
 * ownership comment says, and a completion at the deadline is kept */
static void sc_usb_notify(void)
{
	int64_t r;

	/* Active on the queued receive: ownership kept, nothing resent, nothing dispatched */
	usb_clean();
	poll_once();
	CK(usb_out_owned == 1 && usb_rx_queued == 1 && ev_sends == 1, "usb_notify_queued");
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 2; ev_status = UsbDeviceTransferStatusActive;
	strcpy((char *)rx_store, "a"); fb_calls = 0; poll_once();
	CK(usb_out_owned == 1 && usb_rx_queued == 1 && usb_out_lost == 0 && fb_calls == 0, "usb_notify_active_keeps");
	ev_msg = UsbDeviceEventNoEvent; poll_once();
	CK(ev_sends == 1, "usb_notify_active_no_resend");
	/* a status outside the four documented values keeps the same way */
	ev_status = 7; ev_msg = UsbDeviceEventTransferNotification; poll_once();
	CK(usb_out_owned == 1 && usb_rx_queued == 1 && usb_out_lost == 0 && fb_calls == 0, "usb_notify_unknown_keeps");
	/* the single verified terminal notification ends the request and dispatches once */
	ev_status = -1; ev_bytes = 2; strcpy((char *)rx_store, "x"); poll_once();
	CK(usb_out_owned == 0 && usb_rx_queued == 0 && fb_calls == 1, "usb_notify_complete_once");
	/* a notification naming the other endpoint leaves the queued request alone */
	usb_clean(); poll_once();
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_IN; ev_bytes = 2; poll_once();
	CK(usb_out_owned == 1 && usb_rx_queued == 1 && ev_sends == 1, "usb_notify_other_endpoint");
	/* a buffer the loader did not submit: stopped, unread, never resent */
	usb_clean(); poll_once();
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 2; ev_buf = tx_store; fb_calls = 0; poll_once();
	CK(usb_out_lost == 1 && fb_calls == 0, "usb_notify_wrong_buffer");
	ev_msg = UsbDeviceEventNoEvent; ev_sends = 0; poll_once();
	CK(ev_sends == 0, "usb_notify_wrong_buffer_no_resend");
	/* more bytes completed than submitted, and an event too small to check: both stop */
	usb_clean(); poll_once();
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 600;
	poll_once();
	CK(usb_out_lost == 1 && fb_calls == 0, "usb_notify_long_bytes");
	usb_clean(); poll_once();
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 2; ev_size_set = 1; ev_size = 8; poll_once();
	CK(usb_out_lost == 1 && fb_calls == 0, "usb_notify_short_size");
	/* a failed handle_event call is no event at all */
	usb_clean(); poll_once();
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_err = 1;
	poll_once();
	CK(usb_out_owned == 1 && usb_rx_queued == 1 && usb_out_lost == 0, "usb_notify_handle_error");
	ev_err = 0; ev_msg = UsbDeviceEventNoEvent; ev_sends = 0; poll_once();
	CK(ev_sends == 0, "usb_notify_handle_error_no_resend");
	/* an IN request still Active when the held budget runs out stays owned, never isolated */
	usb_clean();
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_IN; ev_status = UsbDeviceTransferStatusActive; r = usb_tx((const uint8_t *)"OKAYactv", 8);
	CK(r == -1 && usb_in_owned == 1 && usb_in_lost == 0 && ev_sends == 1 && ev_calls <= 60, "usb_notify_active_in_timeout");
	/* a completion arriving past the three-second mark is still taken, not thrown away */
	usb_clean();
	ev_step = clk_freq / 2; ev_no_events = 6;   /* the seventh call lands beyond the budget */
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_IN; ev_bytes = 8;
	r = usb_tx((const uint8_t *)"OKAYtime", 8);
	CK(r == 8 && usb_in_owned == 0 && usb_in_lost == 0, "usb_notify_deadline_kept");
	/* the same budget still bounds a wait that sees no completion */
	usb_clean();
	CK(usb_wait(USB_EP_IN, 1) == -1 && ev_calls <= 60, "usb_notify_deadline_bounded");
	/* a late IN notification that finds no request releases nothing and stops nothing */
	usb_clean();
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_IN; ev_bytes = 8;
	CK(usb_wait(USB_EP_IN, 1) == -1 && usb_in_owned == 0 && usb_in_lost == 0, "usb_notify_late_duplicate");
}

/* guards: a transfer event too short to read isolates every owned request whichever value its
 * unreadable endpoint byte holds; a short DeviceStateChange leaves the cable flag alone; a late
 * event after an isolation changes nothing; a queued request is taken over only by the caller
 * holding its recorded buffer and length */
static void sc_usb_guard(void)
{
	int64_t r;

	for (int k = 0; k < 3; k++) {   /* endpoint byte IN, OUT, IN; sizes 3, 1, 0 */
		usb_clean(); poll_once();
		ev_msg = UsbDeviceEventTransferNotification; ev_ep = k == 1 ? USB_EP_OUT : USB_EP_IN; ev_bytes = 2;
		ev_size_set = 1; ev_size = k == 2 ? 0 : 3 - 2 * k; fb_calls = 0; poll_once();
		CK(usb_out_lost == 1 && usb_out_owned == 1 && fb_calls == 0, "usb_guard_short_event");
	}
	/* the same short event seen by a waiting IN request stops it too, endpoint byte aside */
	usb_clean();
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 2; ev_size_set = 1; ev_size = 2; r = usb_tx((const uint8_t *)"OKAYshrt", 8);
	CK(r == -1 && usb_in_lost == 1 && usb_in_owned == 1 && ev_sends == 1, "usb_guard_short_in_wait");
	/* a DeviceStateChange too short to carry its state leaves the cable flag as it was */
	usb_clean(); poll_once();
	ev_msg = UsbDeviceEventDeviceStateChange; ev_size_set = 1; ev_size = 2;
	CK(usb_wait(USB_EP_OUT, 1) == -1 && usb_connected == 1 && usb_out_lost == 0 && usb_out_owned == 1, "usb_guard_short_state_kept");
	/* after an isolation the matching CompleteOK dispatches nothing and releases nothing */
	usb_clean(); poll_once();
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 2; ev_buf = tx_store; fb_calls = 0; poll_once();
	CK(usb_out_lost == 1 && usb_out_owned == 1 && usb_rx_queued == 1 && fb_calls == 0, "usb_guard_buf_stops");
	ev_buf = NULL; poll_once();
	CK(fb_calls == 0 && usb_out_owned == 1 && usb_rx_queued == 1 && usb_out_lost == 1, "usb_guard_lost_ignores_late_complete");
	ev_msg = UsbDeviceEventNoEvent; ev_sends = 0; poll_once();
	CK(ev_sends == 0, "usb_guard_lost_no_resend");
	/* Cancelled ends an IN wait as a failure and clears its record; the next wait sees none */
	usb_clean();
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_IN; ev_status = UsbDeviceTransferStatusCancelled;
	r = usb_tx((const uint8_t *)"OKAYcanc", 8);
	CK(r == -1 && usb_in_lost == 0 && usb_in_owned == 0, "usb_wait_cancel_clears");
	ev_status = -1;
	CK(usb_wait(USB_EP_IN, 1) == -1 && usb_in_owned == 0 && usb_in_lost == 0 && ev_calls <= 60, "usb_duplicate_ok_noop");
	/* foreign parameters may not take over the queued request: refused, record and ownership kept */
	usb_clean(); poll_once();
	r = usb_rx(rx_store, 64, 0);
	CK(r == -1 && usb_rx_queued == 1 && usb_out_owned == 1 && usb_out_lost == 0 && ev_sends == 1, "usb_guard_reuse_refused");
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 300; strcpy((char *)rx_store, "long"); fb_calls = 0; poll_once();
	CK(fb_calls == 1 && usb_out_lost == 0 && usb_rx_queued == 0, "usb_guard_reuse_record_kept");
	/* the caller holding the recorded buffer and length takes the queued request over */
	usb_clean(); poll_once();
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 4; strcpy((char *)rx_store, "take");
	r = usb_rx(rx_store, 511, 0);
	CK(r == 4 && usb_rx_queued == 0 && usb_out_owned == 0 && ev_sends == 1, "usb_guard_reuse_takeover");
}

int main(void)
{
	sc_psci_spec(); sc_cpu_on(); sc_commands(); sc_enter(); sc_watch(); sc_return(); sc_collect();
	sc_write_logs(); sc_usb(); sc_usb_owner(); sc_usb_recover(); sc_usb_idle_command(); sc_usb_command_interleaved(); sc_fb(); sc_usb_notify(); sc_usb_guard(); sc_fault(); sc_repeat();
	if (fail_count == 0) { printf("production logic: lifecycle checks passed\n"); } else { printf("%d failures\n", fail_count); }
	return fail_count > 127 ? 127 : fail_count;
}

"""
