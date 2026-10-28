#!/usr/bin/env python3
"""Host test for the loader kernel-lifecycle protection.

The marked blocks in xnuloader/loader.c (LC, LC-WRITE, LC-ENTER, LC-WATCH, LC-RETURN,
LC-USB, LC-FB, LC-POLL, LC-EXC, LC-TAIL) are extracted verbatim and compiled into a stub harness (the C text lives in tools/test-loader-lifecycle-harness.py), so the
checks run production control flow and count production side effects: psci calls, the
log-partition query, block writes, hc_take_back, watchdog pets, kernel-memory reads, USB
sends and the event loop, including the verified-notification rule of the LC-USB and
LC-POLL blocks (statuses, buffer and length matching, the deadline order, the rev6 guards:
a short transfer event stops every owned request, a stopped endpoint answers every later event
with nothing, and only the recorded buffer and length may take over a queued receive).
The review-2 rules run as full sequences in sc_usb_dyn: the owned record vetoes every resubmit
path, a timeout or disconnect marks the request undeliverable so its late terminal only
releases it, the link wait (endpoint 0xff) ends on Connected so the command loop submits, and
an overlong Cancelled isolates instead of releasing.
The wait is bounded in every state (three seconds held, running or faulted, ten in a plain
command loop). A stale IN request can ask for one abort; only the shared verified terminal
rule releases its buffer. sc_usb_recover checks OUT delivery while IN is pending or isolated,
unavailable/failed aborts, malformed cancellations, normal completions, stable buffer
identity and bounded repeated waits. Idle command receives remain deliverable after repeated
wait timeouts; abandoned data receives still discard late bytes. PSCI expectations are raw spec numbers written independently
(TF-A include/lib/psci/psci.h, PSCI_RET_*), asserted against the constants inside the
extracted block, so a wrong constant fails instead of passing on a shared value.
The USB event/status/link enums and the event union are injected verbatim from
xnuloader/efi.h and pinned there by _Static_assert against raw numbers written from the
ABL interface the header documents (review L10); counterexamples swap the link-state order
and strip raw[8] from the injected text, and the compile must fail.
Coverage note: the IRQ save/restore assembly and the note/IRQ-mask passes stay inline in
run_on_other_cpu (assembly needs the target; host checks pin call sites and order) and the
host never executes the restore path (held_irq_masked stays 0). In the exception handler the
one IRQ mask instruction becomes a counted host no-op; the handler itself runs, with the
reset and poll-loop escapes routed through setjmp.
Usage: python3 tools/test-loader-lifecycle.py"""

import importlib.util
import re
import subprocess
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOADER = os.path.join(ROOT, "xnuloader", "loader.c")
EFIH = os.path.join(ROOT, "xnuloader", "efi.h")
BUILD = os.environ.get("TMPDIR", "/tmp")
BLOCKS = ("LC", "LC-WRITE", "LC-IRQ", "LC-ENTER", "LC-WATCH", "LC-TAIL", "LC-RETURN", "LC-RESET", "LC-USB", "LC-FB",
          "LC-POLL", "LC-EXC")

def read(path):
    with open(path) as f:
        return f.read()

def read_tu(path):
    """xnuloader/loader.c as the compiler sees it: its own fragments inlined in include order.

    The marked blocks live in those fragments; reassembling the text keeps every extraction
    and every mutation anchor working on the same text the compiler gets, with the blocks
    where the source says they are."""
    src = read(path)
    base = os.path.dirname(path)
    return re.sub(r'^#include "(loader_[A-Za-z0-9_]+\.inc)"\n',
                  lambda m: read(os.path.join(base, m.group(1))), src, flags=re.M)

def extract(src, name):
    m = re.search(r"/\* %s-BEGIN.*?%s-END \*/" % (name, name), src, re.S)
    if not m:
        sys.exit("block %s-BEGIN not found in loader.c" % name)
    return m.group(0)

def macro_value(src, name):
    m = re.search(r"#define\s+%s\s+\(?([0-9xA-Fa-fu]+)\)?" % name, src)
    if not m:
        sys.exit("macro %s not found" % name)
    return m.group(1).rstrip("u")

def struct_copy(src, name):
    m = re.search(r"struct %s \{.*?\n\};" % name, src, re.S)
    if not m:
        sys.exit("struct %s not found" % name)
    return m.group(0)

_spec = importlib.util.spec_from_file_location(
    "lifecycle_harness", os.path.join(os.path.dirname(os.path.abspath(__file__)), "test-loader-lifecycle-harness.py"))
_harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)
_spec2 = importlib.util.spec_from_file_location(
    "lifecycle_harness2", os.path.join(os.path.dirname(os.path.abspath(__file__)), "test-loader-lifecycle-harness2.py"))
_harness2 = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(_harness2)
_spec3 = importlib.util.spec_from_file_location(
    "lifecycle_harness3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "test-loader-lifecycle-harness3.py"))
_harness3 = importlib.util.module_from_spec(_spec3)
_spec3.loader.exec_module(_harness3)
PRELUDE = _harness.PRELUDE
SCENARIOS = (_harness.SCENARIOS.replace("static void ret_reset(int64_t *states)", _harness2.AFF_SCENARIO + "\nstatic void ret_reset(int64_t *states)")
             .replace("static void sc_usb_notify(void)", _harness2.USB2_SCENARIO + "\nstatic void sc_usb_notify(void)")
             .replace("sc_psci_spec();", "sc_affinity(); sc_reset_matrix(); sc_psci_spec();")
             .replace("sc_usb_guard(); sc_fault();", "sc_usb_guard(); sc_usb_dyn(); sc_fault();")
             .replace("static void sc_repeat(void)", _harness3.C_GROUP + "\nstatic void sc_repeat(void)")
             .replace("sc_fault(); sc_repeat();", "sc_fault(); sc_repeat(); cg_run_all();"))


MUTATIONS = [
	("mut_nowatch_started", "watch_", "\t\t\t*status = OTHER_CPU_NOT_STARTED;\n\t\t\treturn 1;   /* held at once: the charger and the storage are not touched this round */", "\t\t\t*status = OTHER_CPU_NOT_STARTED;"),
	("mut_nowatch_hung", "watch_", "\t\t\t*status = OTHER_CPU_HUNG;\n\t\t\treturn 1;   /* held at once: no block, USB or charger service past the decision */", "\t\t\t*status = OTHER_CPU_HUNG;"),
	("mut_samecore", "same_", "\tkernel_resources_held = cores_off ? 0 : 1;  /* proof or no proof: the only release point */", "\tif (ran_other_core) {\n\t\tkernel_resources_held = cores_off ? 0 : 1;\n\t}"),
	("mut_whitelist", "onissue_", "\treturn status == PSCI_E_NOT_SUPPORTED || status == PSCI_E_INVALID_PARAMS ||\n\t    status == PSCI_E_DENIED || status == PSCI_E_INVALID_ADDRESS;", "\treturn status != 0;"),
	("mut_already_on_const", "psci_spec_", "#define PSCI_E_ALREADY_ON       (-4)", "#define PSCI_E_ALREADY_ON       (-6)"),
	("mut_write_gate", "wlog_", "\tbio = ((kernel_resources_held || firmware_fault) && !active_watch) ? NULL : find_log_partition();", "\tbio = (kernel_resources_held && !active_watch) ? NULL : find_log_partition();"),
	("mut_usb_bound", "usb_idle_bounded_exit", "\t\tuint64_t limit = (running_live != NULL || kernel_resources_held || firmware_fault)\n\t\t    ? 3 * freq : USB_IDLE_WAIT_SECONDS * freq;", "\t\tuint64_t limit = (running_live != NULL || kernel_resources_held || firmware_fault)\n\t\t    ? 3 * freq : ~(uint64_t)0;   /* mutation: the plain command loop waits for ever again */"),
	("mut_enter_early", "bridge_", "\tif (status == 0) {\n\t\t/* the exemption covers the formal watch this answer opens", "\tif (1) {   /* rev1 bug: open on every answer */\n\t\t/* the exemption covers the formal watch this answer opens"),
	("mut_exc_keep", "fault_entry_revokes", "\tactive_watch = 0;\n\tif (kernel_resources_held) {\n\t\theld_irq_mask();", "\tif (kernel_resources_held) {\n\t\theld_irq_mask();"),
	("m_exc_entry_mask", "fault_entry_masks", "\t\theld_irq_mask();\n\t\tfirmware_fault = 1;", "\t\tfirmware_fault = 1;"),
	("m_exc_free_path", "fault_free", "\tif (kernel_resources_held || firmware_fault) {\n\t\t/* the same rule as reboot", "\tif (1) {\n\t\t/* the same rule as reboot"),
	("m_watch_notstarted_mask", "watch_notstarted_masks_first", "\t\t\theld_irq_mask();   /* the hold starts at the decision: nothing on this path runs first */\n", ""),
	("m_watch_hung_mask", "watch_hung_masks_first", "\t\t\theld_irq_mask();   /* before this round's watchdog call and before any service */\n", ""),
	("m_enter_no_mask", "bridge_", "\t} else if (kernel_resources_held) {", "\t} else if (0) {"),
	("m_tail_no_mask", "tail_held_masks", "\tif (kernel_resources_held || firmware_fault) {\n\t\t/* interrupts stay off while", "\tif (0) {\n\t\t/* interrupts stay off while"),
	("m_watch_pet_swap", "watch_notstarted_no_services", "@WATCH_PET_SWAP@"),
	("m_timeout_mark", "usb_rx_", "\t\t\tif (usb_out_owned && usb_command_bytes < 0) {\n\t\t\t\tusb_out_invalid = 1;\n\t\t\t}\n\t\t\tif (usb_in_owned) {\n\t\t\t\tusb_in_invalid = 1;\n\t\t\t}\n", ""),
	("mut_poll_guard", "usb_poll_", "\tif (!usb_rx_queued && !usb_out_lost && !usb_out_owned && usb_connected", "\tif (!usb_rx_queued && usb_connected"),
	("m_prequeue_owned_guard", "usb_poll_", " && !usb_out_owned && usb_connected &&", " && usb_connected &&"),
	("mut_fb_pre", "fb_", "\tfor (; *code && n < sizeof(msg) - 1; code++) {\n\t\tmsg[n++] = (uint8_t)*code;\n\t}\n\tfor (; *text && n < sizeof(msg) - 1; text++) {\n\t\tmsg[n++] = (uint8_t)*text;\n\t}\n\tusb_tx((const uint8_t *)msg, n);", "\tfor (; *code; code++) {\n\t\tusb_tx_buf[n++] = (uint8_t)*code;\n\t}\n\tfor (; *text && n < 63; text++) {\n\t\tusb_tx_buf[n++] = (uint8_t)*text;\n\t}\n\tusb_tx(usb_tx_buf, n);"),
	("mut_ev_active", "usb_notify_", '\tif (data->transfer.status == UsbDeviceTransferStatusActive ||\n\t    data->transfer.status > UsbDeviceTransferStatusCompleteError) {\n\t\treturn 0;   /* the controller may still work on the buffer: no read, no release, no resend */\n\t}', '\tif (data->transfer.status == UsbDeviceTransferStatusActive ||\n\t    data->transfer.status > UsbDeviceTransferStatusCompleteError) {\n\t\tif (out) { usb_out_owned = 0; usb_rx_queued = 0; } else { usb_in_owned = 0; }   /* rev4: every notification ended the request */\n\t\treturn 0;\n\t}'),
	("mut_ev_lostguard", "usb_guard_", '\tif (out ? usb_out_lost : usb_in_lost) {\n\t\treturn 0;   /* isolated keeps its buffer and its flags: a late event changes nothing */\n\t}\n', ""),
	("mut_ev_shortstop", "usb_guard_", '\tif (usb_out_owned || usb_rx_queued) {\n\t\tusb_stop_endpoint(1, "event too short to attribute");\n\t}\n\tif (usb_in_owned) {\n\t\tusb_stop_endpoint(0, "event too short to attribute");\n\t}', "	(void)0;"),
	("mut_ev_record", "usb_guard_", '\tif (usb_rx_queued) {\n\t\t/* the pending request stays exactly as it was submitted: only a caller using the\n		 * recorded buffer and length may take it over */\n		if (buf != usb_out_buf || len != usb_out_len) {\n			return -1;      /* refused: the on-file request and its ownership are untouched */\n		}\n		usb_rx_queued = 0;      /* (every command is received the same way, into the same buffer) */\n	} else {\n		if (usb_out_owned) {\n			/* the record alone says the buffer is with the controller (a timeout or a\n			 * disconnect left it there): sending again would hand the same buffer twice */\n			return usb_wait(USB_EP_OUT, 1);   /* wait out the terminal that will release it */\n		}\n		if (EFI_ERROR(USB->send(USB_EP_OUT, len, buf))) {\n			return -1;\n		}\n		usb_out_owned = 1;      /* the submit record is written only for a send that succeeded */\n		usb_out_buf = buf;      /* the notification is only believed for this buffer and length */\n		usb_out_len = len;\n		usb_command_bytes = command_wait ? 0 : -1;\n		usb_out_invalid = 0;    /* a new request starts with a fresh delivery state */\n	}\n', '\tif (usb_rx_queued) {\n		usb_rx_queued = 0;   /* rev5: any caller took it over and rewrote the metadata */\n	} else if (EFI_ERROR(USB->send(USB_EP_OUT, len, buf))) {\n		return -1;\n	}\n	usb_out_owned = 1;\n	usb_out_buf = buf;\n	usb_out_len = len;\n'),
	("m_owned_veto", "dyn_", "\t\tif (usb_out_owned) {\n\t\t\t/* the record alone says the buffer is with the controller (a timeout or a\n\t\t\t * disconnect left it there): sending again would hand the same buffer twice */\n\t\t\treturn usb_wait(USB_EP_OUT, 1);   /* wait out the terminal that will release it */\n\t\t}\n", ""),
	("m_disconnect_clears_records", "usb_disconnect", "\t\t\t\t\tif (usb_out_owned) {\n\t\t\t\t\t\tusb_out_invalid = 1;\n\t\t\t\t\t}\n\t\t\t\t\tif (usb_in_owned) {\n\t\t\t\t\t\tusb_in_invalid = 1;\n\t\t\t\t\t}\n", "\t\t\t\t\tusb_out_owned = 0; usb_rx_queued = 0;   /* mutation: the disconnect drops the records */\n\t\t\t\t\treturn -1;   /* a disconnect ends this wait; the records stay on file */"),
	("m_disconnect_mark", "dyn_", "\t\t\t\t\tif (usb_out_owned) {\n\t\t\t\t\t\tusb_out_invalid = 1;\n\t\t\t\t\t}\n\t\t\t\t\tif (usb_in_owned) {\n\t\t\t\t\t\tusb_in_invalid = 1;\n\t\t\t\t\t}\n", ""),
	("m_connected_clears_owned", "usb_relink", "\t\t\t\tint was_connected = usb_connected;\n\t\t\t\tusb_connected = (data.device_state == UsbDeviceStateConnected);\n\t\t\t\tif (data.device_state != UsbDeviceStateConnected) {", "\t\t\t\tint was_connected = usb_connected;\n\t\t\t\tusb_connected = (data.device_state == UsbDeviceStateConnected);\n\t\t\t\tusb_out_owned = 0;   /* mutation: a reconnect forgets the record */\n\t\t\t\tif (data.device_state != UsbDeviceStateConnected) {"),
	("m_connected_clears_lost", "usb_relink", "\t\t\t\tint was_connected = usb_connected;\n\t\t\t\tusb_connected = (data.device_state == UsbDeviceStateConnected);\n\t\t\t\tif (data.device_state != UsbDeviceStateConnected) {", "\t\t\t\tint was_connected = usb_connected;\n\t\t\t\tusb_connected = (data.device_state == UsbDeviceStateConnected);\n\t\t\t\tusb_out_lost = 0;   /* mutation: a reconnect ends the isolation */\n\t\t\t\tif (data.device_state != UsbDeviceStateConnected) {"),
	("m_link_wait_exit", "dyn_", "\t\t\t\tif (!was_connected && endpoint == 0xff) {\n\t\t\t\t\t/* a link wait exists to learn of the connection: the command loop must\n\t\t\t\t\t * return and submit, holding every record exactly as it stands */\n\t\t\t\t\treturn 0;\n\t\t\t\t}\n", ""),
	("m_poll_disconnect_mark", "dyn_", "\t\t\t\tif (usb_out_owned) {\n\t\t\t\t\tusb_out_invalid = 1;\n\t\t\t\t}\n\t\t\t\tif (usb_in_owned) {\n\t\t\t\t\tusb_in_invalid = 1;\n\t\t\t\t}\n", ""),
	("m_connected_poll_clears", "usb_poll_", '\t\treturn;   /* the cable moved: the queue flag and the ownership records stay as they are */', '\t\tusb_out_owned = 0;   /* mutation: the cable event clears the record */\n\t\treturn;'),
	("m_cancel_success", "usb_wait_cancel", '\t\t\t\t\tif (kept == 1 && deliver) {\n\t\t\t\t\t\treturn (int64_t)bytes;\n\t\t\t\t\t}', '\t\t\t\t\tif ((kept == 1 || kept == 2) && deliver) {\n\t\t\t\t\t\treturn (int64_t)bytes;   /* mutation: Cancelled answers like a completion */\n\t\t\t\t\t}'),
	("m_ignore_other", "usb_cancel_frees", '\t\tusb_notification(0, &data, &n);\n\t\treturn;\n\t}\n\tif (data.transfer.endpoint_index != USB_EP_OUT) {', '\t\treturn;   /* mutation: the IN terminal is dropped unread */\n\t}\n\tif (data.transfer.endpoint_index != USB_EP_OUT) {'),
	("m_cancel_bound", "dyn_", "\t\tif (data->transfer.bytes_completed > sub_len) {\n\t\t\tusb_stop_endpoint(out, \"Cancelled over a length that was not submitted\");\n\t\t\treturn -1;   /* overlong like an over-OK: structurally unsound, nothing releases */\n\t\t}\n", ""),
	("m_isolation_frees", "usb_guard_buf", '\t\t\tusb_out_lost = 1;\n\t\t}', '\t\t\tusb_out_lost = 1;\n\t\t\tusb_out_owned = 0;   /* mutation: the isolation forgets the buffer */\n\t\t}'),
	("m_recover_no_age", "recover_young_kept", "\tif (!usb_in_invalid && usb_in_since != 0 && age < USB_IN_STALE_SECONDS) {\n\t\treturn -1;      /* young: its transfer may still be running, nothing to recover */\n\t}\n", ""),
	("m_poll_data_purpose", "interleave_poll_records_command_purpose", "\t\tusb_command_bytes = 0;\n\t\tusb_out_invalid = 0;", "\t\tusb_command_bytes = -1;\n\t\tusb_out_invalid = 0;"),
	("m_early_command_drop", "interleave_out_completion_saved", "usb_command_bytes = (int64_t)bytes;", "usb_command_bytes = 0;"),
	("m_command_idle_drop", "idle_command_wait_preserves_delivery", "if (usb_out_owned && usb_command_bytes < 0)", "if (usb_out_owned)"),
	("m_data_late_deliver", "idle_data_timeout_stays_invalid", "if (usb_out_owned && usb_command_bytes < 0)", "if (0)"),
	("m_recover_no_abort", "recover_abort_requested_once", "\t(void)USB->abort_xfer(USB_EP_IN);", "\t/* mutation: no abort request */"),
	("m_recover_forgets_record", "recover_abort_requested_once", "\tusb_in_abort_requested = 1;", "\tusb_in_abort_requested = 1;\n\tusb_in_owned = 0;"),
	("m_recover_repeat_abort", "recover_repoll_no_repeat_abort", "usb_in_lost || usb_in_abort_requested", "usb_in_lost"),
	("mut_ev_deadline", "usb_notify_", "@DEADLINE_SWAP@"),
	("m_aff_negative_pending", "ret_bad", '\t\t\tif (state == 0 || state == 2) {   /* on, or being started: the race may still settle */', '\t\t\tif (state != 1) {   /* mutation: an error is polled like a settling race */'),
	("m_aff_reask_off", "ret_aff_settles", '\t\t\tif (off_seen[i]) {\n\t\t\t\tcontinue;   /* an off core is never asked again */\n\t\t\t}\n', ''),
	("m_aff_budget_per_core", "ret_aff_shared", '\tuint64_t budget = freq / 10;   /* 100 ms of counter ticks, shared: never a per-core timer */', '\tuint64_t budget = freq / 10 * (uint64_t)cpu_count;   /* mutation: a budget per core */'),
	("m_aff_no_issue_gate", "ret_aff_shared", '\t\t\tif (READ_SYSREG(CNTVCT_EL0) - began > budget) {\n\t\t\t\twaiting = 1;   /* the budget stands on no further query */\n\t\t\t\tcontinue;\n\t\t\t}\n', ''),
	("m_aff_nofreq_skip", "ret_nofreq", '\tif (!polling && cpu_count > first) {\n\t\tprint("the counter runs at no frequency: cannot tell whether the cores are off\\n");\n\t\tcores_off = 0;\n\t}\n', '\t(void)polling;\n'),
]

def build_c(src):
    blocks = []
    for name in BLOCKS:
        txt = extract(src, name)
        if name == "LC-ENTER":
            txt = "static void\nrun_enter(void)\n{\n" + txt + "\n}\n"
        elif name == "LC-IRQ":
            # the mask is one AArch64 instruction pair: the host counts it instead of running it
            txt = txt.replace('__asm__ volatile ("msr daifset, #0xf\\nmsr S3_0_C12_C12_7, %0\\nisb" : : "r" ((uint64_t)0));',
                              "harness_irq_mask();")
            if "harness_irq_mask();" not in txt:
                sys.exit("LC-IRQ block: the mask instruction moved, update the test")
        elif name == "LC-EXC":
            txt = txt.replace('__asm__ volatile ("msr daifset, #0xf");', "harness_daifset();")
            txt = txt.replace("harness_daifset();\n\tfor (;;) {\n\t}",
                "harness_daifset();\n\twhile (halt_armed) { halt_armed = 0; longjmp(jb_exit, 3); }")
        elif name == "LC-TAIL":
            # the restore writes two AArch64 registers: the host counts it instead of running it
            txt = txt.replace(
                '__asm__ volatile ("msr S3_0_C12_C12_7, %0\\nisb" : : "r" (held_group1));\n'
                '\t\t__asm__ volatile ("msr daif, %0" : : "r" (held_daif));',
                "harness_irq_restore();")
            if "harness_irq_restore();" not in txt:
                sys.exit("LC-TAIL block: the restore instructions moved, update the test")
            txt = "static int64_t\nrun_tail(int64_t status)\n{\n" + txt + "\n}\n"
        elif name == "LC-POLL":
            txt = ("static void\npoll_once(void)\n{\n\tuint32_t msg = UsbDeviceEventNoEvent;\n\tuint64_t size = 0, n;\n"
                   "\tusb_device_event_data data;\n\tchar cmd[128];\n\n\tif (USB == NULL) {\n\t\treturn;\n\t}\n"
                   + txt + "\n}\nstatic void usb_poll_command(void) { poll_once(); }\n")
        blocks.append(txt)
    prelude = PRELUDE
    exc = re.search(r"enum \{ EXC_CTX_.*?\};", read(EFIH), re.S).group(0)
    usb = re.search(r"enum \{ UsbDeviceEventNoEvent.*?\} usb_device_event_data;", read(EFIH), re.S).group(0)
    for token, value in (("@C_PRELUDE@", _harness3.C_PRELUDE), ("@START@", macro_value(src, "START_SECONDS")), ("@SILENCE@", macro_value(src, "SILENCE_SECONDS")),
                         ("@FAULT_SERVE@", macro_value(src, "FAULT_SERVE_SECONDS")),
                         ("@SELFTEST@", macro_value(src, "CHECKPOINT_SELFTEST")), ("@MAGIC2@", macro_value(src, "KERNEL_LIVE_MAGIC2")),
                         ("@LIVE@", struct_copy(src, "kernel_live")), ("@EXC@", exc), ("@USB@", usb),
                         ("@DESCSET@", re.search(r"typedef struct \{[^}]*\} usb_device_descriptor_set;",
                                                 read(EFIH), re.S).group(0)),
                         ("@PSCI@", _harness2.PSCI_STUB)):
        prelude = prelude.replace(token, value)
    return prelude + "\n" + "\n".join(blocks) + "\n" + SCENARIOS + "\n"

def deadline_swap(src):
    """budget-before-completion mutant: swap the in-loop event handling with the budget
    and pet block. Both slices live inside the same for(;;) scope, so the swap always
    compiles; a completion that arrives at the deadline is thrown away by the mutant."""
    a0 = src.index("\t\t/* a completion in hand is verified before the budget")
    mid = src.index("\t\t/* the wait is bounded in every state", a0)
    a1 = src.index("\t\tif (READ_SYSREG(CNTVCT_EL0) - last_pet > freq) {\n\t\t\tlast_pet = READ_SYSREG(CNTVCT_EL0);\n\t\t\twdt_pet();\n\t\t}\n", mid)
    a1 += len("\t\tif (READ_SYSREG(CNTVCT_EL0) - last_pet > freq) {\n\t\t\tlast_pet = READ_SYSREG(CNTVCT_EL0);\n\t\t\twdt_pet();\n\t\t}\n")
    ev, rest = src[a0:mid], src[mid:a1]
    return ev + rest, rest + ev

def watch_pet_swap(src):
    """the round's watchdog pet moved ahead of the two timeout decisions: a path that is
    about to hold would then pet once after deciding (and, on a real core, before masking)."""
    a = src.index("\tif (!*arrived) {\n\t\tflush_dcache((uint64_t)(uintptr_t)&other_cpu_stage")
    b = src.index("\t/* Feed this core's watchdog through the requested budget")
    c = src.index("\t/* Qualcomm's UEFI charger stack owns the PMIC/GLINK side")
    decisions, pet = src[a:b], src[b:c]
    return decisions + pet, pet + decisions

def compile_run(c_text, name):
    cpath = os.path.join(BUILD, "lifecycle-%s.c" % name)
    xpath = os.path.join(BUILD, "lifecycle-%s" % name)
    with open(cpath, "w") as f:
        f.write(c_text)
    r = subprocess.run(["cc", "-std=gnu11", "-O0", "-o", xpath, cpath], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("compile of %s failed:\n%s" % (name, r.stderr))
    try:
        r = subprocess.run([xpath], capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        # a mutation that breaks a loop bound leaves no FAIL line at all; the hang itself
        # is the signal, and the suite must not stall on it
        return 124, "HUNG: %s ran past the scenario budget" % name
    return r.returncode, r.stdout

def compile_fails(c_text, name):
    cpath = os.path.join(BUILD, "lifecycle-%s.c" % name)
    with open(cpath, "w") as f:
        f.write(c_text)
    r = subprocess.run(["cc", "-fsyntax-only", "-std=gnu11", cpath], capture_output=True, text=True)
    return r.returncode, r.stderr

def usb_abi_counterexamples(src):
    base = build_c(src)
    cases = (
        ("state enum order swapped", "enum { UsbDeviceStateConnected, UsbDeviceStateDisconnected };",
         "enum { UsbDeviceStateDisconnected, UsbDeviceStateConnected };"),
        ("event union without raw[8]", "\tuint64_t raw[8];\n", ""),
    )
    for label, old, new in cases:
        if old not in base:
            sys.exit("counterexample source changed: %s" % label)
        rc, err = compile_fails(base.replace(old, new, 1), "usbabi")
        if rc == 0 or "static assertion" not in err:
            sys.exit("counterexample not caught (%s): the asserts do not pin the %s" % (label, label))
        print("counterexample check: %-28s caught by _Static_assert" % label)

def wiring_checks(src):
    fails = []

    def ck(cond, name):
        if not cond:
            fails.append("FAIL wiring_%s" % name)

    def i_of(s, start=0):
        return src.find(s, start)

    ck(src.count("kernel_hold") == 0, "no_old_state_name")
    ck(not re.search(r"\bLC_NONE\b|\bLC_HELD\b|\bLC_UNKNOWN\b", src), "merged_states")
    for gone in ("lc_kernel_may_still_own", "lc_cpu_on_begin", "lc_cpu_on_end", "lc_watch_end", "lc_return_release"):
        ck(gone not in src, "gone_" + gone)
    ck(src.count("#define PSCI_CPU_ON ") == 1, "cpu_on_id_once")
    ck(src.count("static int kernel_resources_held;") == 1, "hold_once")
    ck(src.count("static int active_watch;") == 1, "watch_once")
    blk_e = extract(src, "LC-ENTER")
    ck("status = lc_cpu_on_issue(mpidr, (uint64_t)(uintptr_t)&other_cpu_entry, (uint64_t)(uintptr_t)&other_cpu_start);" in blk_e, "enter_issue")
    ck(0 < blk_e.find("if (status == 0) {") < blk_e.find("active_watch = 1;") < blk_e.find("flush_log();"), "enter_gate_order")
    i_aw0 = i_of("active_watch = 0;")
    i_live = i_of("running_live = NULL;   /* the log pointer drops here")
    ck(0 < i_of("while (status == 0) {") < i_aw0 < i_live, "watch_close_order")
    blk_x = extract(src, "LC-EXC")
    ck(0 < blk_x.find("active_watch = 0;") < blk_x.find("print(\"\\nUEFI EXCEPTION"), "exc_first_act")
    # the hold's mask and the fault record belong to the held entry; the annibale USB branch
    # is the fault state itself and records one before serving
    i_entry = blk_x.find("if (kernel_resources_held) {")
    ck(0 < i_entry < blk_x.find("held_irq_mask();", i_entry) < blk_x.find("firmware_fault = 1;", i_entry)
       < blk_x.find("print(\"\\nUEFI EXCEPTION"), "exc_mask_order")
    ck(blk_x.find("if (PLAT == &platform_annibale && screen_quiet) {")
       < blk_x.find("firmware_fault = 1;", blk_x.find("if (PLAT == &platform_annibale"))
       < blk_x.find("compose_logs(NULL, 0);"), "exc_branch_marks_fault_before_compose")
    # the held or faulted path takes PSCI and nothing else; only the path that never handed
    # anything over keeps the cold reset with its log flush
    i_psci = blk_x.find("psci_reset_attempt();")
    i_flush = blk_x.find("flush_log();")
    i_reset = blk_x.find("ST->rt->reset_system(0 /* EfiResetCold */, 0, 0, NULL);")
    ck(0 < i_psci < i_flush < i_reset, "exc_psci_before_fallback")
    i_held = blk_x.find("if (kernel_resources_held || firmware_fault) {")
    held_path = blk_x[i_held:i_flush]
    ck(0 < i_held < i_psci and "flush_log" not in held_path and "reset_system" not in held_path,
       "exc_held_path_no_flush_no_reset")
    ck(blk_x.find("psci_reset_attempt();") < blk_x.find("set_result(\"reset-failed\""), "exc_psci_records_failure")
    # the serve window has one bound: the counter; a platform without a frequency is served
    # for zero rounds instead of a round count standing in for the missing clock
    ck(blk_x.count("usb_poll_command();") == 1 and "if (freq == 0) {" in blk_x
       and "while (READ_SYSREG(CNTVCT_EL0) - began <= FAULT_SERVE_SECONDS * freq) {" in blk_x,
       "exc_serve_bounded")
    ck(0 < blk_x.find("set_result(\"firmware-fault\"") < blk_x.find("compose_logs(NULL, 0);")
       < blk_x.find("usb_poll_command();"), "exc_serve_after_record")
    blk_w = extract(src, "LC-WRITE")
    ck("((kernel_resources_held || firmware_fault) && !active_watch) ? NULL : find_log_partition()" in blk_w, "write_gate_line")
    ck(src.count("find_log_partition()") == 1, "one_partition_query")
    blk_u = extract(src, "LC-USB")
    ck("? 3 * freq : USB_IDLE_WAIT_SECONDS * freq;" in blk_u and blk_u.count("began > limit") == 1,
       "usb_bound_line")
    ck(blk_u.count('usb_abort_stale_in("') == 2 and "static int\nusb_abort_stale_in(const char *why)" in blk_u,
       "recover_defined_and_called_twice")
    ck(0 < blk_u.find('usb_abort_stale_in("a reply")') < blk_u.find("memcpy(usb_tx_buf"), "recover_before_reply")
    ck(0 < blk_u.find('usb_abort_stale_in("the next command")') < blk_u.find("if (usb_out_lost) {"),
       "recover_before_command")
    ck("usb_in_since = READ_SYSREG(CNTVCT_EL0);" in blk_u and blk_u.count("usb_in_since") >= 3, "submit_stamp")
    ck("USB->set_endpoint_stall_state(" not in blk_u and "USB->start_ex(" not in blk_u
       and "USB->allocate_transfer_buffer(" not in blk_u, "recover_keeps_buffer_identity")
    ck("usb_in_recoveries >= USB_IN_RECOVER_LIMIT" in blk_u, "recover_bounded")
    ck("n = usb_rx(usb_rx_buf, 511, 1);" in src and "int64_t r = usb_rx(usb_rx_buf, want, 0);" in src,
       "command_and_data_receive_modes")
    ck("USB_CHUNK    (1024 * 1024)" in src and "#define USB_CHUNK (1024 * 1024)" in PRELUDE, "chunk_mirror")
    ck("queues a fresh receive" not in src, "no_recovery_overclaim")
    ck(0 < blk_u.find("if (usb_out_lost) {") < blk_u.find("USB->send(USB_EP_OUT, len, buf)"), "rx_guard_first")
    ck(0 < blk_u.find("if (usb_in_lost) {") < blk_u.find("memcpy(usb_tx_buf"), "tx_guard_first")
    blk_p = extract(src, "LC-POLL")
    ck("!usb_rx_queued && !usb_out_lost && !usb_out_owned && usb_connected" in blk_p, "poll_lost_guard")
    ck("usb_notification(0, &data, &n);" in blk_p, "poll_books_in_terminal")
    ck("usb_notification(1, &data, &n) != 1" in blk_p, "poll_confirms")
    ck("static int\nusb_notification(" in blk_u, "shared_rule_defined")
    ck("usb_notification(data.transfer.endpoint_index == USB_EP_OUT, &data, &bytes)" in blk_u, "wait_uses_shared_rule")
    ck(0 < blk_u.find("usb_notification(data.transfer.endpoint_index ==") < blk_u.find("began > limit"), "completion_before_deadline")
    ck(blk_u.count("data_buffer != sub") == 1 and "data->transfer.status > UsbDeviceTransferStatusCompleteError" in blk_u, "verify_checks_present")
    # usb_notification bounds the byte count of each terminal branch separately; the two
    # checks serve different paths and both must stay:
    #   Cancelled branch: the bound runs after the status, buffer and length-record checks
    #     and before the record release. An overlong Cancelled is structurally unsound: the
    #     endpoint is isolated, the record and buffer stay owned, the call returns -1.
    #   CompleteOK branch: the bound runs before *bytes is written and the record released.
    #     An overlong completion delivers no bytes, the endpoint is isolated and the record
    #     stays owned.
    # Each branch returns its own verdict (2: cancelled without bytes, 1: bytes delivered),
    # so the Cancelled path leaves before the CompleteOK code runs and one bound cannot serve
    # both terminals. Two occurrences mean one bound per terminal; the chain pins each inside
    # its branch, after the status gate and before the release/delivery it must precede.
    i_cancel_gate = blk_u.find("if (data->transfer.status == UsbDeviceTransferStatusCancelled)")
    i_cancel_bound = blk_u.find('"Cancelled over a length that was not submitted"')
    i_cancel_release = blk_u.find("usb_out_owned = 0;    /* the request ended as a failure")
    i_ok_gate = blk_u.find("if (data->transfer.status != UsbDeviceTransferStatusCompleteOK)")
    i_ok_bound = blk_u.find('"completed more bytes than submitted"')
    i_delivery = blk_u.find("*bytes = data->transfer.bytes_completed;")
    ck(blk_u.count("bytes_completed > sub_len") == 2, "length_bounds_per_terminal")
    ck(0 < i_cancel_gate < i_cancel_bound < i_cancel_release < i_ok_gate < i_ok_bound < i_delivery,
       "length_bound_order")
    ck(blk_u.count("usb_out_buf = buf;") == 1 and blk_u.count("usb_in_buf = usb_tx_buf;") == 1
       and "usb_out_buf = usb_rx_buf;" in blk_p, "record_submit")
    ck("a failed call delivered no event to read" in blk_u and "no event to act on" in blk_p, "handle_error_gated")
    ck(0 < blk_u.find("usb_out_lost : usb_in_lost") < blk_u.find("!usb_out_owned"), "lost_guard_first")
    i_wait = blk_u.find("static int64_t\nusb_wait(uint8_t endpoint, int deliver)")
    i_rx = blk_u.find("static int64_t\nusb_rx(")
    wait_body = blk_u[i_wait:i_rx]
    ck(0 <= i_wait < i_rx and 0 <= wait_body.find("if (size < sizeof(data.transfer))")
       < wait_body.find("data.transfer.endpoint_index =="), "wait_size_before_fields")
    ck(0 <= blk_p.find("if (size < sizeof(data.transfer))") < blk_p.find("data.transfer.endpoint_index !="), "poll_size_before_fields")
    ck(0 <= blk_u.find("if (size >= sizeof(data.device_state))") < blk_u.find("data.device_state =="), "wait_size_before_state")
    ck(0 <= blk_p.find("if (size >= sizeof(data.device_state))") < blk_p.find("data.device_state =="), "poll_size_before_state")
    ck(src.count("usb_short_event_stops_all();") == 2 and "static void\nusb_short_event_stops_all(void)" in blk_u, "short_stop_shared")
    ck("buf != usb_out_buf || len != usb_out_len" in blk_u, "reuse_refuses_foreign")
    ck("@USB@" in PRELUDE and "enum { UsbDeviceState" not in PRELUDE, "usb_enums_injected")
    ck("usb_device_event_data) == 64" in PRELUDE and "offsetof(usb_device_event_data, transfer.data_buffer) == 16" in PRELUDE, "usb_layout_asserts")
    blk_f = extract(src, "LC-FB")
    ck(src.count("usb_tx_buf[") == 0, "single_copy_site")
    ck(blk_f.count("n < sizeof(msg) - 1") == 2, "fb_hard_limits")
    ck("usb_tx((const uint8_t *)msg, n);" in blk_f, "fb_via_guarded_tx")
    i_g = i_of("lc_after_return(ran_other_core, cpu_mpidr, cpu_count);")
    ck(0 < i_g < i_of("if (block_write_requested)", i_g), "gate_before_stats")
    ck('set_result(kernel_resources_held ? "returned-held" : "returned", klog_len, 0);' in src, "held_result")
    for s in ('kernel_resources_held ? "hung-held"', 'kernel_resources_held ? "no-such-core-held"',
              'kernel_resources_held ? "selftest-ok-held"', 'kernel_resources_held ? "checkpoint-held"',
              'kernel_resources_held ? "early-exception-held"'):
        ck(s in src, "held_suffix")
    ck(src.count("psci_reset_attempt();") == 3 and src.count("psci_call(PSCI_SYSTEM_RESET") == 1, "one_reset_helper_three_doors")
    i = src.find("reboot(void)\n{")
    ck(0 < i and i < src.find("psci_reset_attempt();", i) < src.find('hc_take_back("before the reset")', i), "reboot_psci_first")
    i = src.find("exception_handler(int64_t type")
    handler = src[i:src.find("install_exception_hook", i)]
    ck(0 < i and 0 < handler.find("psci_reset_attempt();") < handler.find("flush_log();")
       < handler.find("reset_system"), "fault_psci_first")
    ck('"  ldr x9, [x10, #64]\\n  msr daif, x9\\n"' not in src, "stub_no_early_daif")
    i = src.find("--- the kernel came back")
    ck(0 < i and src.find("held_daif = loader_ctx[CTX_DAIF];", i) < src.find("if (!held_irq_masked) {\n\t\theld_daif", i + 1) or
       0 < i < src.find("held_daif = loader_ctx[CTX_DAIF];", i) < src.find("wdt_set_ms(WDT_BITE_MS);", i), "snapshot_before_watchdog")
    i = src.find("if (loader_save_ctx() == 0) {")
    ck(0 < i and i < src.find("kernel_resources_held = 1;", i) < src.find("get_memory_map", i), "held_set_before_clean")
    ck("lc_cmd_denied(kernel_resources_held, firmware_fault, live, cmd)" in src, "deny_call")
    ck("lc_stop_action(kernel_resources_held, live)" in src, "stop_call")
    ck("AGENTS.md 2" in src[:4000], "overlimit_note")
    blk_watch = extract(src, "LC-WATCH")
    ck("*status = OTHER_CPU_NOT_STARTED;\n\t\t\treturn 1;" in blk_watch, "notstarted_returns")
    ck("*status = OTHER_CPU_HUNG;\n\t\t\treturn 1;" in blk_watch, "hung_returns")
    # C group: both timeout decisions mask before anything else of the round, so the hold's
    # mask cannot land after a service, a pet or the caller's log write
    i_ns = blk_watch.find("*status = OTHER_CPU_NOT_STARTED;")
    i_hung = blk_watch.find("*status = OTHER_CPU_HUNG;")
    ck(0 < blk_watch.find("held_irq_mask();", 0, i_ns) < i_ns, "notstarted_masks_first")
    ck(0 < blk_watch.find("held_irq_mask();", i_ns, i_hung) < i_hung, "hung_masks_first")
    ck(0 < blk_watch.find("held_irq_mask();") < blk_watch.find("wdt_pet();"), "pet_after_mask_in_round")
    ck(0 < blk_watch.find("held_irq_mask();") < blk_watch.find("charger_poll();")
       < blk_watch.find("block_poll();") < blk_watch.find("usb_poll_command();"), "mask_before_services")
    blk_i = extract(src, "LC-IRQ")
    ck("static void\nheld_irq_mask(void)" in blk_i and blk_i.count("held_irq_masked = 1;") == 1, "one_mask_helper")
    ck(blk_i.find("if (!held_irq_masked) {") < blk_i.find("held_daif = daif;"), "mask_keeps_first_record")
    ck(blk_i.count("msr daifset, #0xf") == 1 and blk_i.count("msr S3_0_C12_C12_7") == 1, "mask_both_registers")
    # the four held or early-exit paths share the helper: annibale's entry, the NOTE_SECONDS
    # pass, an answer outside the refusal whitelist, and the loop tail before status returns
    i_run = src.find("run_on_other_cpu(uint64_t mpidr")
    i_ret = src.find("return status;", i_run) + len("return status;")
    body = src[i_run:i_ret]
    ck(body.count("held_irq_mask();") == 4, "run_paths_share_mask")
    i_issue = body.find("lc_cpu_on_issue(")
    i_flush = body.find("flush_log();", i_issue)
    ck(0 < i_issue < body.find("held_irq_mask();", i_issue) < i_flush, "enter_masks_before_flush")
    ck("} else if (kernel_resources_held) {" in blk_e, "enter_masks_only_when_held")
    # the loop tail: the mask sits inside the held/fault guard and before the status return
    i_guard = body.rfind("if (kernel_resources_held || firmware_fault) {")
    i_tail = body.find("held_irq_mask();", i_guard) if i_guard > 0 else -1
    ck(0 < i_guard < i_tail < body.find("return status;", i_guard), "tail_masks_before_return")
    ck("LC-TAIL-BEGIN" in src and src.find("return status;", i_run) < src.find("LC-TAIL-END", i_run),
       "tail_extracted_whole")
    ck(body.count("READ_SYSREG(DAIF)") == 0 and body.count("held_daif = loader_ctx[CTX_DAIF]") == 0, "entry_no_local_record")
    blk_r = extract(src, "LC-RETURN")
    ck("uint64_t budget = freq / 10;" in blk_r, "shared_budget_once")
    ck(blk_r.count("psci_call(PSCI_AFFINITY_INFO") == 1, "one_affinity_site")
    ck(blk_r.count("READ_SYSREG(CNTVCT_EL0) - began > budget") == 2, "budget_gates_issue_and_tail")
    ck("Stall" not in blk_r and "WFI" not in blk_r, "affinity_polls_without_stall")
    ck(blk_r.find("off_seen[i] = 1") < blk_r.find("kernel_resources_held = cores_off"), "off_answer_cached")
    blk_ret = extract(src, "LC-RETURN")
    ck(0 < blk_ret.find("kernel_resources_held = cores_off ? 0 : 1;") < blk_ret.find('hc_take_back("the kernel came back")'), "release_before_takeback")
    ck(blk_ret.count("msr S3_0_C12_C12_7") == 1, "restore_asm_in_gate")
    ck("if (!kernel_resources_held && held_irq_masked) {" in src, "restore_gated")
    return fails

def main():
    src = read_tu(LOADER)
    rc, out = compile_run(build_c(src), "prod")
    print(out.rstrip())
    if rc != 0:
        sys.exit("production build failed")
    for entry in MUTATIONS:
        name, tag = entry[0], entry[1]
        if entry[2] == "@DEADLINE_SWAP@":
            old, new = deadline_swap(src)
        elif entry[2] == "@WATCH_PET_SWAP@":
            old, new = watch_pet_swap(src)
        else:
            old, new = entry[2], entry[3]
        patched = src.replace(old, new, 1)
        if patched == src:
            sys.exit("mutation %s: source text changed, update the test" % name)
        rc, mout = compile_run(build_c(patched), name)
        lines = [l for l in mout.splitlines() if l.startswith("FAIL")]
        if rc == 124:
            print("mutation check: %-22s caught by hang (scenario budget expired)" % name)
            continue
        if rc == 0:
            sys.exit("mutation %s survived: the tests miss it" % name)
        if not any(l.startswith("FAIL " + tag) for l in lines):
            sys.exit("mutation %s failed unexpectedly: %s" % (name, mout))
        print("mutation check: %-22s caught by %d checks (%s...)" % (name, len(lines), lines[0]))
    usb_abi_counterexamples(src)
    fails = wiring_checks(src)
    for f in fails:
        print(f)
    if fails:
        sys.exit("wiring checks failed")
    print("wiring checks passed; the loader translation unit is %d lines" % (src.count(chr(10)) + 1))

if __name__ == "__main__":
    main()
