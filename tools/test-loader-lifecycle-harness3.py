"""C group (held / interrupt mask / exception) harness text for tools/test-loader-lifecycle.py.

C_PRELUDE is spliced into the stub prelude before the clock helper, C_GROUP into the scenario
section before main(). Split out of test-loader-lifecycle-harness.py to keep each file under
the 1000-line limit (AGENTS.md 2); the driver concatenates all three into one C file.
"""

C_PRELUDE = r"""/* ---- C group: the interrupt hold record and the ordered call record ---- */
#define DAIF 2
#define S3_0_C12_C12_7 3
#define FAULT_SERVE_SECONDS @FAULT_SERVE@
/* the two registers the mask saves: distinguishable values so the restore can be checked
 * against them, never equal to a clock reading */
static uint64_t fake_daif = 0x3c0, fake_group1 = 1;
static int irq_mask_calls, irq_restore_calls;
/* one letter per firmware-facing call the held rules constrain, written when the call happens,
 * so a scenario asserts the mask came before a flush, a pet or a PSCI call instead of reading
 * source text. 'P' is any PSCI call, 'M' a mask, 'R' a restore, 'F' a partition query,
 * 'C' a log compose, 'W' a watchdog pet, 'T' a storage hand-back. */
static char ev_seq[64];
static int ev_seq_n;
static void seq(char c) { if (ev_seq_n < (int)sizeof(ev_seq) - 1) { ev_seq[ev_seq_n++] = c; ev_seq[ev_seq_n] = 0; } }
static void seq_clear(void) { ev_seq_n = 0; ev_seq[0] = 0; }
static void harness_irq_mask(void) { irq_mask_calls++; seq('M'); }    /* the LC-IRQ mask, counted */
static void harness_irq_restore(void) { irq_restore_calls++; seq('R'); }
static void harness_daifset(void) { irq_mask_calls++; seq('M'); }   /* the halt's own mask */
"""

C_GROUP = r"""/* ---- C group scenarios: the held mask order, the fault path, the cross-core tail ---- */
static void sc_held_order(void)
{
	static struct kernel_live live;
	live.heartbeat = 1;
	/* an answer outside the refusal whitelist is held, and the mask precedes the log work this
	 * bridge does; a whitelisted refusal starts nothing and masks nothing */
	PLAT = &platform_annibale;
	psci_next = -1;
	kernel_resources_held = active_watch = 0;
	irq_mask_calls = 0; seq_clear();
	run_enter();
	/* the letters: 'P' the CPU_ON call itself, then the two log composes with the partition
	 * query between them (write_logs composes before and after writing) */
	CK(status == -1 && kernel_resources_held == 0 && irq_mask_calls == 0 && !strcmp(ev_seq, "PCFC"),
	   "bridge_whitelist_no_mask");
	psci_next = 7;
	kernel_resources_held = active_watch = 0;
	irq_mask_calls = 0; held_irq_masked = 0; seq_clear();
	run_enter();
	CK(status == 7 && kernel_resources_held == 1 && irq_mask_calls == 1, "bridge_unknown_masks");
	CK(!strcmp(ev_seq, "PMC"), "bridge_unknown_mask_before_log");
	psci_next = -4;
	kernel_resources_held = active_watch = 0;
	irq_mask_calls = 0; seq_clear();
	run_enter();
	CK(ev_seq[0] == 'P' && ev_seq[1] == 'M' && ev_seq[2] == 'C', "bridge_already_on_masks_before_log");
	kernel_resources_held = 0;
	/* both timeout decisions of the round mask first: one mask, no pet, and the two registers
	 * of the moment are on file for the release point */
	watch_reset(6000ULL * clk_freq);
	other_cpu_stage = arrived_v = 0;
	clk_now += (START_SECONDS + 1) * clk_freq;
	irq_mask_calls = 0; held_irq_masked = 0; seq_clear();
	CK(wr(&live, 6000ULL * clk_freq) == 1, "notstarted_stops_again");
	CK(irq_mask_calls == 1 && !strcmp(ev_seq, "M") && pets == 0, "watch_notstarted_masks_first");
	CK(held_irq_masked == 1 && held_daif == fake_daif && held_group1 == fake_group1, "watch_notstarted_holds_record");
	watch_reset(7000ULL * clk_freq);
	clk_now += 2 * clk_freq;
	CK(wr(&live, 7000ULL * clk_freq) == 0, "hung_first_round_again");
	pets = irq_mask_calls = 0; held_irq_masked = 0; seq_clear();
	clk_now += (SILENCE_SECONDS + 1) * clk_freq;
	CK(wr(&live, 7000ULL * clk_freq) == 1, "hung_stops_again");
	CK(irq_mask_calls == 1 && !strcmp(ev_seq, "M") && pets == 0, "watch_hung_masks_first");
	held_irq_masked = 0;
	kernel_resources_held = 1;
}

static void sc_fault_path(void)
{
	static uint64_t ctxb[128];
	/* annibale quiet screen while held: the entry masks and records before the serve loop,
	 * and the exemption is already revoked when that loop starts taking commands */
	USB = &h_usb; usb_connected = 1;
	usb_rx_queued = usb_out_owned = usb_in_owned = usb_out_lost = usb_in_lost = usb_out_invalid = usb_in_invalid = 0;
	ev_step = ev_change = 0;
	PLAT = &platform_annibale; screen_quiet = 1;
	active_watch = kernel_resources_held = 1;
	firmware_fault = 0; held_irq_masked = 0;
	running_live = (const struct kernel_live *)0x1000;
	find_calls = w_blocks = fb_calls = irq_mask_calls = 0; seq_clear();
	fb_armed = 1;
	ev_msg = UsbDeviceEventTransferNotification; ev_ep = USB_EP_OUT; ev_bytes = 2;
	strcpy((char *)rx_store, "x");
	if (setjmp(jb_exit) == 0) {
		exception_handler(11, ctxb);
		CK0("fault_annibale_returned");
	}
	fb_armed = 0;
	CK(fb_calls == 1, "fault_poll_serves_commands");
	CK(irq_mask_calls == 1 && held_irq_masked == 1, "fault_entry_masks");
	CK(active_watch == 0 && running_live == NULL, "fault_entry_revokes_and_drops");
	CK(firmware_fault == 1, "fault_entry_records_fault");
	CK(!strncmp(last_result, "firmware-fault", 14), "fault_records_result");
	CK(find_calls == 0 && w_blocks == 0, "fault_no_storage");
	/* another platform, held: entry mask, then the PSCI attempt, then the masked halt; no log
	 * flush and no UEFI reset callback on this path */
	PLAT = &plat_two; screen_quiet = 0;
	active_watch = kernel_resources_held = 1;
	firmware_fault = 0; held_irq_masked = 0;
	find_calls = res_calls = psci_calls = irq_mask_calls = 0; seq_clear();
	psci_next = -3;
	halt_armed = 1;
	if (setjmp(jb_exit) == 0) {
		exception_handler(11, ctxb);
		CK0("fault_held_returned");
	}
	halt_armed = 0;
	CK(!strcmp(ev_seq, "MPM") && irq_mask_calls == 2, "fault_held_mask_psci_halt");
	CK(psci_calls == 1 && psci_fn == PSCI_SYSTEM_RESET, "fault_held_psci");
	CK(res_calls == 0 && find_calls == 0, "fault_held_no_flush_no_reset");
	CK(!strncmp(last_result, "reset-failed", 12), "fault_held_records_failure");
	/* nothing was handed over: the original cold reset with its log flush stays the behaviour,
	 * and this path masks nothing and records no fault */
	PLAT = &plat_two;
	screen_quiet = 0;
	kernel_resources_held = active_watch = firmware_fault = 0;
	bio_present = 1;
	find_calls = w_blocks = res_calls = psci_calls = irq_mask_calls = 0;
	held_irq_masked = 0; seq_clear();
	snprintf(last_result, sizeof(last_result), "untouched");
	psci_next = -1;
	if (setjmp(jb_exit) == 0) {
		exception_handler(11, ctxb);
		CK0("fault_free_returned");
	}
	CK(irq_mask_calls == 0 && firmware_fault == 0 && held_irq_masked == 0, "fault_free_no_mask_no_fault");
	CK(find_calls == 1 && w_blocks > 0, "fault_free_flushes_log");
	CK(res_calls == 1 && psci_calls == 0, "fault_free_uefi_reset");
	CK(!strncmp(last_result, "untouched", 9), "fault_free_no_psci_result");
	/* the USB fault window is bounded by the counter; without a frequency the handler serves
	 * nothing instead of inventing a round bound for a clock it does not have */
	PLAT = &platform_annibale; screen_quiet = 1;
	kernel_resources_held = active_watch = firmware_fault = 0;
	ev_step = 100ULL * clk_freq;
	ev_calls = psci_calls = res_calls = irq_mask_calls = 0;
	halt_armed = 1; psci_next = 0;
	cap_reset();
	if (setjmp(jb_exit) == 0) {
		exception_handler(11, ctxb);
		CK0("fault_window_returned");
	}
	halt_armed = 0;
	CK(ev_calls >= 3 && ev_calls <= 5, "fault_window_bounded_by_counter");
	CK(cap_has("window closed after") && psci_calls == 1 && res_calls == 0, "fault_window_then_psci");
	CK(firmware_fault == 1, "fault_window_records_fault");
	clk_freq = 0;
	kernel_resources_held = active_watch = firmware_fault = 0;
	ev_step = 0; ev_calls = psci_calls = res_calls = 0;
	halt_armed = 1; psci_next = 0;
	cap_reset();
	if (setjmp(jb_exit) == 0) {
		exception_handler(11, ctxb);
		CK0("fault_nofreq_returned");
	}
	halt_armed = 0;
	clk_freq = 1000000000ULL;
	CK(ev_calls == 0 && cap_has("cannot be bounded") && psci_calls == 1 && res_calls == 0, "fault_nofreq_serves_nothing");
	firmware_fault = 0;
}

static void sc_tail(void)
{
	/* the loop tail after a round decided for the hold: mask before the caller writes */
	kernel_resources_held = 1; firmware_fault = 0;
	active_watch = 1; running_live = (const struct kernel_live *)0x1000;
	held_irq_masked = 0; irq_mask_calls = 0; seq_clear();
	CK(run_tail(1) == 1, "tail_held_returns_status");
	CK(irq_mask_calls == 1 && !strcmp(ev_seq, "M") && held_irq_masked == 1, "tail_held_masks");
	CK(active_watch == 0 && running_live == NULL, "tail_clears_watch_first");
	/* a recorded fault keeps the same path even when the hold was already released */
	kernel_resources_held = 0; firmware_fault = 1;
	held_irq_masked = 0; irq_mask_calls = 0; seq_clear();
	CK(run_tail(2) == 2 && irq_mask_calls == 1 && held_irq_masked == 1, "tail_fault_masks");
	/* a refused request started nothing: the recorded state is handed back, not masked again */
	firmware_fault = 0;
	held_irq_masked = 1; held_daif = 0x3c0; held_group1 = 7;
	irq_mask_calls = irq_restore_calls = 0; seq_clear();
	CK(run_tail(-1) == -1, "tail_refused_returns_status");
	CK(irq_restore_calls == 1 && !strcmp(ev_seq, "R") && held_irq_masked == 0, "tail_refused_restores");
	CK(irq_mask_calls == 0, "tail_refused_no_mask");
	/* nothing held and nothing masked: no firmware call at all */
	held_irq_masked = 0; irq_mask_calls = irq_restore_calls = 0; seq_clear();
	CK(run_tail(0) == 0 && irq_mask_calls == 0 && irq_restore_calls == 0 && ev_seq_n == 0, "tail_free_quiet");
}

static void cg_run_all(void)
{
	sc_held_order();
	sc_fault_path();
	sc_tail();
}
"""