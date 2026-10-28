#!/usr/bin/env python3
"""Second half of the loader lifecycle harness (AGENTS.md 2: files stay under 800 lines).
The driver (test-loader-lifecycle.py) splices PSCI_STUB into the PRELUDE at @PSCI@ and
inserts AFF_SCENARIO before main(). It holds the per-core PSCI affinity stub and the
AFFINITY shared-budget scenarios of the USB/return review section 5."""

PSCI_STUB = r"""#define PSCI_SYSTEM_RESET 0x84000009u
/* the cores this scenario answers for, and what each one says when asked whether it is
 * off: rounds re-read the table, so a scenario can script settle (flip after N answers),
 * a permanent on, or an undocumented value. Each query burns psci_tick counter ticks:
 * the SMC is not preemptible, and the shared budget only gates issuing the next one. */
static uint64_t cpus[8] = { 0x81000000ULL, 0x80000100ULL, 0x80000200ULL, 0, 0, 0, 0, 0 };
static int64_t psci_aff_ans[8], psci_aff_flip_to, psci_next;
static int psci_aff_seen[8], psci_aff_flip_n, psci_tick, halt_armed;
static int64_t psci_call(uint64_t f, uint64_t a1, uint64_t a2, uint64_t a3)
{
	psci_calls++;
	seq('P');
	psci_fn = f, psci_a1 = a1, psci_a2 = a2, psci_a3 = a3;
	if (f == PSCI_AFFINITY_INFO) {
		int k = 0;

		clk_now += psci_tick;
		while (k < 8 && cpus[k] != 0 && cpus[k] != a1) {
			k++;
		}
		if (k == 8 || cpus[k] == 0) {
			return -999;   /* nobody scripted an answer for this core */
		}
		psci_aff_seen[k]++;
		if (psci_aff_flip_n && psci_aff_seen[k] > psci_aff_flip_n) {
			return psci_aff_flip_to;
		}
		return psci_aff_ans[k];
	}
	return psci_next;
}
"""

AFF_SCENARIO = r"""/* the shared-budget rules of return review section 5: off is cached, on and pending settle
 * inside the budget, errors hold at once, a missing counter frequency asks nothing, and the
 * eight cores share one budget instead of getting eight */
static void aff_knobs(void)
{
	clk_freq = 1000000000ULL;
	psci_tick = (int)(clk_freq / 33);   /* about 30 ms per in-flight query: rounds stay finite */
	for (int i = 0; i < 8; i++) {
		psci_aff_ans[i] = 1;
		psci_aff_seen[i] = 0;
	}
	psci_aff_flip_n = 0;
}
static void sc_affinity(void)
{
	int64_t all_off[8] = { 1, 1, 1, 1, 1, 1, 1, 1 };

	kernel_resources_held = 1;
	takebacks_n = held_irq_masked = 0;
	aff_knobs();
	psci_calls = 0;
	lc_after_return(1, cpus, 3);
	CK(psci_calls == 3 && kernel_resources_held == 0, "ret_off_one_query");
	/* core 2 answers on, then pending, then off: the wait settles inside the budget */
	aff_knobs();
	psci_aff_ans[2] = 0;
	psci_aff_flip_n = 1;
	psci_aff_flip_to = 1;
	psci_calls = 0;
	takebacks_n = 0;
	lc_after_return(1, cpus, 3);
	CK(kernel_resources_held == 0 && takebacks_n == 1, "ret_aff_settles_on");
	aff_knobs();
	psci_aff_ans[2] = 2;
	psci_aff_flip_n = 1;
	psci_aff_flip_to = 1;
	psci_calls = 0;
	takebacks_n = 0;
	lc_after_return(1, cpus, 3);
	CK(kernel_resources_held == 0 && takebacks_n == 1, "ret_aff_settles_pending");
	/* a permanent on is retried until the shared budget runs out: held, no takeback */
	aff_knobs();
	psci_aff_ans[1] = 0;
	psci_tick = (int)(clk_freq / 500);   /* 2 ms per query: many retries fit the budget */
	psci_calls = 0;
	takebacks_n = 0;
	lc_after_return(1, cpus, 3);
	CK(kernel_resources_held == 1 && takebacks_n == 0, "ret_aff_never_off_holds");
	/* eight cores share one budget: the gate stops issuing while cores wait, never eight times */
	for (int i = 3; i < 8; i++) {
		cpus[i] = 0x80000300ULL + 0x100 * i;
	}
	aff_knobs();
	for (int i = 0; i < 8; i++) {
		psci_aff_ans[i] = 0;
	}
	psci_tick = (int)(clk_freq / 50);   /* 20 ms per query: six issue inside 100 ms */
	psci_calls = 0;
	takebacks_n = 0;
	lc_after_return(1, cpus, 8);
	CK(kernel_resources_held == 1 && psci_calls == 6, "ret_aff_shared_budget");
	for (int i = 3; i < 8; i++) {
		cpus[i] = 0;
	}
	/* a counter with no frequency cannot bound the wait: hold at once without asking */
	aff_knobs();
	clk_freq = 0;
	psci_tick = (int)(1000000000ULL / 33);
	psci_calls = 0;
	takebacks_n = 0;
	cap_reset();
	lc_after_return(1, cpus, 3);
	CK(kernel_resources_held == 1 && psci_calls == 0 && takebacks_n == 0, "ret_nofreq_no_query");
	CK(cap_has("no frequency"), "ret_nofreq_prints");
	/* the counter wrapped between the snapshot and a query: unsigned distance still bounds */
	aff_knobs();
	clk_now = ~0ull - clk_freq / 200;   /* half a budget before the wrap */
	psci_tick = (int)(clk_freq / 50);   /* 20 ms: three queries cross the wrap */
	psci_calls = 0;
	takebacks_n = 0;
	lc_after_return(1, cpus, 3);
	CK(kernel_resources_held == 0 && psci_calls == 3, "ret_aff_wrap_ok");
	kernel_resources_held = 1;
}
static void sc_reset_matrix(void)
{
	/* the shared attempt passes the firmware answer straight through, asking exactly once
	 * with no arguments: every held or faulted door judges that value the same way */
	psci_next = 0;
	psci_calls = 0;
	CK(psci_reset_attempt() == 0 && psci_calls == 1 && psci_fn == PSCI_SYSTEM_RESET, "reset_pass_through");
	psci_next = -3;
	CK(psci_reset_attempt() == -3 && psci_a1 == 0 && psci_a2 == 0 && psci_a3 == 0, "reset_error_pass");
}
"""

USB2_SCENARIO = r"""
/* the USB/return review section 2 counterexamples as full command-flow sequences: link up
 * from idle, timeout then the old OK, disconnect then reconnect then resubmit, the poll's
 * own old OK, and the overlong Cancelled. Every step drives the real wait, submit and poll
 * bodies; the counters are the production side effects. */
static void sc_usb_dyn(void)
{
	/* idle and disconnected: the link wait must end on Connected so the loop can submit */
	usb_clean(); usb_connected = 0;
	ev_change = 1; ev_change_state = UsbDeviceStateConnected; ev_calls = ev_sends = 0;
	CK(usb_wait(0xff, 0) == 0 && usb_connected == 1 && ev_calls == 1, "dyn_link_wait_ends_on_connected");
	ev_change = 0; ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 2; ev_after = 3;
	strcpy((char *)rx_store, "go");
	CK(usb_rx(rx_store, 511, 0) == 2 && ev_sends == 1, "dyn_first_out_after_link_up");
	/* held: a timed-out OUT keeps the record, marked undeliverable; neither the poll nor a
	 * new usb_rx may send the controller-held buffer again; the old OK only releases */
	usb_clean();
	CK(usb_rx(rx_store, 511, 0) == -1 && usb_out_owned == 1 && usb_out_invalid == 1 && usb_rx_queued == 0
	   && ev_sends == 1, "dyn_timeout_marks_record");
	ev_msg = UsbDeviceEventNoEvent; ev_sends = 0; poll_once();
	CK(ev_sends == 0 && usb_out_owned == 1, "dyn_poll_no_resend_while_owned");
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 3; strcpy((char *)rx_store, "abc");
	CK(usb_rx(rx_store, 511, 0) == -1 && ev_sends == 0 && usb_out_owned == 0 && usb_out_invalid == 0,
	   "dyn_timeout_old_ok_releases_no_delivery");
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 2; strcpy((char *)rx_store, "go"); ev_sends = 0;
	CK(usb_rx(rx_store, 511, 0) == 2 && ev_sends == 1 && usb_out_owned == 0, "dyn_new_submit_after_release");
	/* idle poll pre-queues, the cable drops and comes back: the queued request is not resent,
	 * its old OK releases without a delivery, and only then a fresh receive goes out */
	usb_clean(); kernel_resources_held = 0; poll_once();
	CK(usb_rx_queued == 1 && usb_out_owned == 1 && usb_out_invalid == 0 && ev_sends == 1, "dyn_poll_prequeue_record");
	ev_msg = UsbDeviceEventNoEvent; ev_change = 1; ev_change_state = UsbDeviceStateDisconnected; poll_once();
	CK(usb_connected == 0 && usb_out_invalid == 1 && usb_out_owned == 1, "dyn_disconnect_marks_record");
	ev_change_state = UsbDeviceStateConnected; ev_calls = 0;
	CK(usb_wait(0xff, 0) == 0 && usb_connected == 1, "dyn_reconnect_returns_to_loop");
	ev_change = 0; ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 2;
	strcpy((char *)rx_store, "late"); fb_calls = 0; ev_sends = 0;
	CK(usb_rx(rx_store, 511, 0) == -1 && fb_calls == 0 && ev_sends == 0 && usb_out_owned == 0 && usb_rx_queued == 0,
	   "dyn_reconnect_old_ok_releases_no_resend");
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 1; strcpy((char *)rx_store, "q"); ev_sends = 0;
	CK(usb_rx(rx_store, 511, 0) == 1 && ev_sends == 1 && usb_out_owned == 0, "dyn_new_command_after_reconnect");
	/* the poll's own sequence: disconnect marks, a reconnect keeps the record and sends no
	 * second OUT, and the old OK dispatches no command; the next poll pre-queues freshly */
	usb_clean(); poll_once();
	ev_msg = UsbDeviceEventNoEvent; ev_change = 1; ev_change_state = UsbDeviceStateDisconnected; poll_once();
	CK(usb_out_invalid == 1 && usb_out_owned == 1, "dyn_poll_disconnect_marks_record");
	ev_change_state = UsbDeviceStateConnected; ev_sends = 0; poll_once();
	CK(ev_sends == 0 && usb_connected == 1 && usb_out_invalid == 1, "dyn_poll_reconnect_no_resend");
	ev_change = 0; ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 4;
	strcpy((char *)rx_store, "old"); fb_calls = 0; poll_once();
	CK(fb_calls == 0 && usb_out_owned == 0 && usb_out_invalid == 0, "dyn_poll_old_ok_not_dispatched");
	ev_msg = UsbDeviceEventNoEvent; ev_sends = 0; poll_once();
	CK(ev_sends == 1 && usb_rx_queued == 1 && usb_out_invalid == 0, "dyn_poll_fresh_receive_after_release");
	/* a disconnect that lands inside the wait keeps the record and marks it undeliverable:
	 * the old OK of that request reaches no caller and only releases the buffer */
	usb_clean(); poll_once();
	int64_t r;
	ev_change = 1; ev_change_state = UsbDeviceStateDisconnected;
	r = usb_wait(USB_EP_OUT, 1);
	CK(r == -1 && usb_out_owned == 1 && usb_out_invalid == 1, "dyn_wait_disconnect_marks_record");
	ev_change = 0; ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 2;
	CK(usb_rx(usb_rx_buf, 511, 0) == -1 && usb_out_owned == 0 && usb_out_invalid == 0 && usb_rx_queued == 0,
	   "dyn_wait_disconnect_old_ok_not_delivered");
	/* a Cancelled that reports more bytes than the 511 submitted is structurally unsound:
	 * it isolates instead of releasing; the same record one byte lower releases normally */
	usb_clean(); poll_once();
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_status = UsbDeviceTransferStatusCancelled;
	ev_bytes = 512; fb_calls = 0; poll_once();
	CK(usb_out_lost == 1 && usb_out_owned == 1 && usb_rx_queued == 1 && fb_calls == 0, "dyn_overlong_cancel_isolates");
	usb_clean(); poll_once();
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_status = UsbDeviceTransferStatusCancelled; ev_bytes = 511;
	poll_once();
	CK(usb_out_lost == 0 && usb_out_owned == 0 && usb_rx_queued == 0, "dyn_exact_cancel_releases");
}

static void sc_usb_command_interleaved(void)
{
	/* A queued command remains deliverable after an IN wait exhausts its budget. */
	usb_clean(); poll_once();
	CK(usb_out_owned && usb_command_bytes == 0, "interleave_poll_records_command_purpose");
	CK(usb_tx((const uint8_t *)"OKAY", 4) == -1 && usb_out_owned && !usb_out_invalid,
	   "interleave_in_timeout_preserves_queued_command");
	memcpy(rx_store, "reboot", 6);
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 6;
	CK(usb_rx(rx_store, 511, 1) == 6 && !memcmp(rx_store, "reboot", 6),
	   "interleave_first_reboot_after_in_timeout");

	/* OUT can complete before either the IN terminal or its timeout. Both consumers must
	 * deliver the saved command once, before the receive buffer can be submitted again. */
	for (int consumer = 0; consumer < 2; consumer++) {
		for (int timeout = 0; timeout < 2; timeout++) {
			usb_clean(); poll_once();
			memcpy(rx_store, "reboot", 6);
			ev_prefix_pending = 1;
			ev_prefix.transfer.endpoint_index = USB_EP_OUT;
			ev_prefix.transfer.status = UsbDeviceTransferStatusCompleteOK;
			ev_prefix.transfer.data_buffer = rx_store;
			ev_prefix.transfer.bytes_completed = 6;
			ev_msg = timeout ? UsbDeviceEventNoEvent : UsbDeviceEventTransferNotification;
			ev_ep = USB_EP_IN; ev_bytes = 4;
			CK(usb_tx((const uint8_t *)"OKAY", 4) == (timeout ? -1 : 4)
			    && usb_command_bytes == 6 && !usb_out_owned, "interleave_out_completion_saved");
			int sends = ev_sends, events = ev_calls;
			CK(usb_rx(rx_store, 511, 0) == -1 && usb_command_bytes == 6,
			   "interleave_data_cannot_reuse_ready_command_buffer");
			fb_calls = 0;
			if (consumer) {
				poll_once();
				CK(fb_calls == 1 && !strcmp(fb_got, "reboot"), "interleave_poll_delivers_saved_command");
			} else {
				CK(usb_rx(rx_store, 511, 1) == 6 && !memcmp(rx_store, "reboot", 6),
				   "interleave_rx_delivers_saved_command");
			}
			CK(usb_command_bytes == 0 && ev_sends == sends && ev_calls == events,
			   "interleave_saved_command_consumed_without_resubmit");
			ev_msg = UsbDeviceEventNoEvent;
			poll_once();
			CK(fb_calls == consumer && ev_sends == sends + 1,
			   "interleave_saved_command_not_dispatched_twice");
		}
	}
}
"""
