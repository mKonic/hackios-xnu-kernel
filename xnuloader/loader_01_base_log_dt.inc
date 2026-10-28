/* ------------------------------------------------------------------------ */
/* Fixed conventions shared with the boot shim, the packer and the kernel   */

#define PAYLOAD_BASE        0xF0000000ULL
#define PAYLOAD_MAGIC       0x50554E58u /* "XNUP" */

struct payload_header { uint32_t magic; uint32_t size; };
struct payload_entry { char name[16]; uint32_t size; uint32_t reserved; };

#define KERNEL_VIRT_BASE    0xFFFFFFF004000000ULL /* boot_args.virtBase */
#define KERNEL_LINK_OFFSET  0x03000000ULL         /* kernel text sits 48 MB above the base */
#define BASE_ALIGN          0x02000000ULL         /* 32 MB: L2 block size with 16K pages */
#define MAX_MEM_SIZE        0x180000000ULL        /* 6 GB; the kernel's bootstrap page tables cover 8 */
/*
 * The payload's "memsize" may ask for more, for a kernel whose bootstrap page tables cover it (the
 * xnu-12377 one, for iOS 26 and its 5 GB system image): up to this.
 */
#define MAX_MEM_SIZE_LIMIT  0x380000000ULL        /* 14 GB */
/*
 * A root image too big for the payload area is placed here, at a fixed
 * offset inside the kernel's memory window, once; it then survives any
 * number of kernel runs (nothing writes to it: the root is read-only).
 */
#define RESIDENT_RD_OFFSET  0x08000000ULL
#define RESIDENT_RD_RESERVE 0x30000000ULL         /* RAM that must remain for the kernel itself */
#define PAGE_SIZE_MAX       0x4000ULL

#define LOG_PARTITION       u"logdump"
#define CHECKPOINT_SELFTEST 999
#define PANIC_LOG_SIZE      0x80000u
#define NVRAM_BANK_SIZE     0x2000u
/*
 * Two bounds, two names (review U1): PAYLOAD_RESERVE_SIZE is the RAM the loader keeps
 * out of every window; PAYLOAD_CONTAINER_MAX is the largest complete container the
 * loader downloads or parses (8-byte header + declared body + 16-byte entry padding).
 * The shim, the packers and the host scripts are built against the same numbers.
 */
#define PAYLOAD_RESERVE_SIZE 0x03800000ULL        /* R: [PAYLOAD_BASE, PAYLOAD_BASE + R) stays reserved in the UEFI memory map */
#define PAYLOAD_CONTAINER_MAX 0x03700000ULL       /* C: largest downloaded or parsed container, header included */
#define PANIC_LOG_BASE      0xF3700000ULL         /* inside the reservation, right after the container bound; up to PAYLOAD_BASE + PAYLOAD_RESERVE_SIZE the RAM stays reserved */

/* ------------------------------------------------------------------------ */
/* XNU structures (pexpert/pexpert/arm64/boot.h, mach-o/loader.h)           */

struct boot_video {
	uint64_t v_baseAddr, v_display, v_rowBytes, v_width, v_height, v_depth;
};

#define BOOT_LINE_LENGTH 608

struct boot_args {
	uint16_t revision;
	uint16_t version;
	uint64_t virtBase;
	uint64_t physBase;
	uint64_t memSize;
	uint64_t topOfKernelData;
	struct boot_video video;
	uint32_t machineType;
	uint64_t deviceTreeP;
	uint32_t deviceTreeLength;
	char commandLine[BOOT_LINE_LENGTH];
	uint64_t bootFlags;
	uint64_t memSizeActual;
};

#define MH_MAGIC_64      0xFEEDFACFu
#define LC_SEGMENT_64    0x19
#define LC_UNIXTHREAD    0x5

struct mach_header_64 { uint32_t magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags, reserved; };
struct load_command { uint32_t cmd, cmdsize; };
struct segment_command_64 {
	uint32_t cmd, cmdsize; char segname[16];
	uint64_t vmaddr, vmsize, fileoff, filesize;
	uint32_t maxprot, initprot, nsects, flags;
};

/* ------------------------------------------------------------------------ */
/* Globals and small helpers                                                */

static efi_system_table *ST;
static efi_boot_services *BS;
static efi_handle IMAGE;

static char log_buf[32768];
static uint32_t log_len;

void *
memset(void *dst, int c, size_t n)
{
	uint8_t *d = dst;
	while (n--) {
		*d++ = (uint8_t)c;
	}
	return dst;
}

int
memcmp(const void *a, const void *b, size_t n)
{
	const uint8_t *x = a, *y = b;

	for (; n > 0; n--, x++, y++) {
		if (*x != *y) {
			return *x < *y ? -1 : 1;
		}
	}
	return 0;
}

void *
memcpy(void *dst, const void *src, size_t n)
{
	uint8_t *d = dst;
	const uint8_t *s = src;

	/*
	 * Word at a time when possible: USB transfer buffers are uncached, where
	 * a byte loop costs one bus access per byte (6 MB/s instead of 30+).
	 */
	if ((((uintptr_t)d | (uintptr_t)s) & 7) == 0) {
		uint64_t *dw = (uint64_t *)d;
		const uint64_t *sw = (const uint64_t *)s;

		for (; n >= 32; n -= 32, dw += 4, sw += 4) {
			uint64_t a = sw[0], b = sw[1], c = sw[2], e = sw[3];
			dw[0] = a;
			dw[1] = b;
			dw[2] = c;
			dw[3] = e;
		}
		for (; n >= 8; n -= 8) {
			*dw++ = *sw++;
		}
		d = (uint8_t *)dw;
		s = (const uint8_t *)sw;
	}
	while (n--) {
		*d++ = *s++;
	}
	return dst;
}

static size_t
str_len(const char *s)
{
	size_t n = 0;
	while (s[n]) {
		n++;
	}
	return n;
}

/* LC-STR-BEGIN -- extracted verbatim by tools/test-loader-buildid.py: the comparison every
 * getvar answer uses */
static int
str_eq(const char *a, const char *b)
{
	while (*a && *a == *b) {
		a++, b++;
	}
	return *a == *b;
}
/* LC-STR-END */

/*
 * Set once the loader takes commands over USB: its log travels that way, and
 * the screen is left to the kernel (and to "oem screenshot").
 */
static int screen_quiet;

static void
put_char(char c)
{
	char16 w[3] = { 0, 0, 0 };

	if (log_len < sizeof(log_buf) - 1) {
		log_buf[log_len++] = c;
	}
	if (ST->con_out != NULL && !screen_quiet) {
		if (c == '\n') {
			w[0] = '\r', w[1] = '\n';
		} else {
			w[0] = (char16)c;
		}
		ST->con_out->output_string(ST->con_out, w);
	}
}

/* printf subset: %s %x %d (64-bit values), %% */
static void
print(const char *fmt, ...)
{
	__builtin_va_list ap;
	__builtin_va_start(ap, fmt);
	for (; *fmt; fmt++) {
		if (*fmt != '%') {
			put_char(*fmt);
			continue;
		}
		fmt++;
		if (*fmt == 's') {
			const char *s = __builtin_va_arg(ap, const char *);
			while (*s) {
				put_char(*s++);
			}
		} else if (*fmt == 'x' || *fmt == 'd') {
			uint64_t v = __builtin_va_arg(ap, uint64_t);
			unsigned base = (*fmt == 'x') ? 16 : 10;
			char tmp[24];
			int i = 0;
			do {
				tmp[i++] = "0123456789abcdef"[v % base];
				v /= base;
			} while (v);
			while (i) {
				put_char(tmp[--i]);
			}
		} else {
			put_char(*fmt);
		}
	}
	__builtin_va_end(ap);
}

#define READ_SYSREG(name) ({ uint64_t _v; __asm__ volatile ("mrs %0, " #name : "=r" (_v)); _v; })

/*
 * Write back and drop cached copies of a range. Required for memory the
 * kernel will touch with the MMU (and so the data cache) off: its early code
 * builds page tables that way, and a stale cached copy of those pages would
 * be what the table walker sees once caches are turned on again.
 */
static void
flush_dcache(uint64_t start, uint64_t end)
{
	uint64_t line = 4ULL << ((READ_SYSREG(CTR_EL0) >> 16) & 0xF);
	for (uint64_t p = start & ~(line - 1); p < end; p += line) {
		__asm__ volatile ("dc civac, %0" : : "r" (p));
	}
	__asm__ volatile ("dsb sy");
}

static void
clean_dcache(uint64_t start, uint64_t end)
{
	uint64_t line = 4ULL << ((READ_SYSREG(CTR_EL0) >> 16) & 0xF);
	for (uint64_t p = start & ~(line - 1); p < end; p += line) {
		__asm__ volatile ("dc cvac, %0" : : "r" (p));
	}
	__asm__ volatile ("dsb sy");
}

/* ------------------------------------------------------------------------ */
/* Log persistence: raw write to a GPT partition (phone only)               */

static efi_block_io *
find_partition_named(const char16 *want, unsigned want_len)
{
	static const efi_guid part_guid = EFI_PARTITION_INFO_GUID;
	static const efi_guid blk_guid = EFI_BLOCK_IO_GUID;
	efi_handle *handles;
	uint64_t count;

	if (EFI_ERROR(BS->locate_handle_buffer(ByProtocol, &part_guid, NULL, &count, &handles))) {
		return NULL;
	}
	for (uint64_t i = 0; i < count; i++) {
		efi_partition_info *info;
		efi_block_io *bio;
		int match = 1;

		if (EFI_ERROR(BS->handle_protocol(handles[i], &part_guid, (void **)&info)) || info->type != 2) {
			continue;
		}
		for (unsigned k = 0; k < want_len; k++) {
			if (info->name[k] != want[k]) {
				match = 0;
				break;
			}
		}
		if (match && !EFI_ERROR(BS->handle_protocol(handles[i], &blk_guid, (void **)&bio))) {
			return bio;
		}
	}
	return NULL;
}

/* F09LW-BEGIN -- host-extracted verbatim by /tmp/task19/logwrite_harness.c */
/*
 * Review F09, lead's ruling b: the phone has no logdump partition, and boot_a -- where this
 * used to fall back -- carries the boot image of the slot-boot flow. So the fallback is no
 * longer automatic: only the explicit "oem logwrite" command, this flag set while it runs,
 * lets the log land on boot_a. Every automatic flush with no logdump partition goes nowhere
 * (its kernel log is echoed on the console instead) and the USB side stays pure reads.
 */
static int log_fallback_boot_a_allowed;

static efi_block_io *
find_log_partition(void)
{
	static const char16 want[] = LOG_PARTITION;
	efi_block_io *bio;

	bio = find_partition_named(want, sizeof(want) / sizeof(want[0]));
	if (bio == NULL && log_fallback_boot_a_allowed) {
		static const char16 alt[] = u"boot_a";
		bio = find_partition_named(alt, sizeof(alt) / sizeof(alt[0]));
	}
	return bio;
}
/* F09LW-END */

/*
 * The "staged" buffer holds the loader log followed by the kernel log of the
 * last run. It is what gets written to the log partition and what the host
 * fetches over USB (fastboot get_staged).
 */
static char out_buf[2 * 1024 * 1024];
/* the storage's reads for the kernel land here (block_poll); also where the log is read back */
#define BLOCK_BUFFER_SIZE  (4u << 20)
static uint8_t block_buffer[BLOCK_BUFFER_SIZE] __attribute__((aligned(0x2000)));
static const char *staged_data = out_buf;   /* what "upload" sends: normally the log in out_buf */
static uint64_t staged_len;

static void
compose_logs(const char *klog, uint64_t klog_len)
{
	static const char sep[] = "\n== KERNEL LOG ==\n", end[] = "\n== END OF LOG ==\n";
	uint64_t n = log_len;

	staged_data = out_buf;
	memset(out_buf, 0, sizeof(out_buf));
	memcpy(out_buf, log_buf, log_len);
	if (klog != NULL) {
		if (klog_len > sizeof(out_buf) - n - 8192) {
			klog_len = sizeof(out_buf) - n - 8192;
		}
		memcpy(out_buf + n, sep, sizeof(sep) - 1);
		n += sizeof(sep) - 1;
		memcpy(out_buf + n, klog, klog_len);
		n += klog_len;
		memcpy(out_buf + n, end, sizeof(end) - 1);
		n += sizeof(end) - 1;
	}
	staged_len = n;
}

static int hc_list_is_uefis(void);
static int hc_log_skips;                /* logs not written this run (said twice at most) */
static void hc_print_state(const char *when);
static void hc_take_back(const char *when);
static int skip_ufs_arbitration;        /* annibale: the kernel never shares the UFS controller, set at platform selection */
static int firmware_fault;              /* UEFI exception handler ran (set in exception_handler) */

/* LC-BEGIN -- host-extracted verbatim by tools/test-loader-lifecycle.py: the production kernel
 * lifecycle logic, pure C with no UEFI dependencies so the host test compiles it as it is.
 * kernel_resources_held is separate from running_live, the log pointer: what was started on
 * another core may own memory and devices from the CPU_ON request on, and releases only on
 * proof (ruling 2026-10-25): the kernel returned and every core this run handed to XNU
 * answers AFFINITY_INFO off. A hung or never-arrived kernel keeps the hold, and so does a
 * CPU_ON answer outside the refusal whitelist below. While held the loader keeps to
 * operations that touch no shared hardware: status queries, staged logs, and reboot. The
 * cooperative services inside run_on_other_cpu's watch loop are the one listed exception
 * (active_watch), bounded by the round ending the moment a timeout is decided (LC-WATCH). */
#define OTHER_CPU_NOT_STARTED 1000    /* run_on_other_cpu(): the firmware said yes but the core never arrived */
#define OTHER_CPU_HUNG          1001  /* ... the kernel stopped giving signs of life */

#define LC_STOP_NOTIFY 0              /* a live kernel is told to end (oem stop) */
#define LC_STOP_IDLE 1                /* nothing started: the reply stays the harmless OKAY */
#define LC_STOP_HELD 2                /* held with no live kernel: nothing can be notified, say so */

/* PSCI return codes as the SMC Calling Convention spells them out, the way TF-A defines
 * them in include/lib/psci/psci.h (PSCI_RET_*). tools/test-loader-lifecycle.py asserts
 * every one of these against raw spec numbers of its own, so a wrong constant here cannot
 * pass by being shared with the test. */
#define PSCI_E_NOT_SUPPORTED    (-1)
#define PSCI_E_INVALID_PARAMS   (-2)
#define PSCI_E_DENIED           (-3)
#define PSCI_E_ALREADY_ON       (-4)
#define PSCI_E_ON_PENDING       (-5)
#define PSCI_E_INTERNAL_FAILURE (-6)
#define PSCI_E_NOT_PRESENT      (-7)
#define PSCI_E_DISABLED         (-8)
#define PSCI_E_INVALID_ADDRESS  (-9)

#define PSCI_CPU_ON        0xC4000003u
#define PSCI_SYSTEM_RESET  0x84000009u   /* the reset door the held paths use; psci_call picks SMC or HVC */
#define PSCI_AFFINITY_OFF  1

static int kernel_resources_held;   /* a started kernel may still own memory and devices: gate on this */
static int active_watch;            /* inside run_on_other_cpu's watch loop: the one exception to the gates */

static int64_t psci_call(uint64_t function, uint64_t a1, uint64_t a2, uint64_t a3);
static void set_result(const char *what, uint64_t a, uint64_t b);

static inline int
lc_starts(const char *s, const char *p)
{
	while (*p) {
		if (*s++ != *p++) {
			return 0;
		}
	}
	return 1;
}

/* commands that take over memory or device state, refused while anything runs or is held:
 * the payload window, the kernel start itself, storage writes and the display write */
static inline int
lc_blocked_cmd(const char *cmd)
{
	static const char *const blocked[] = { "oem ramdisk", "download:", "oem run", "oem selftest",
		"oem hang", "oem recording", "oem sound", "oem clearscreen", "oem memmap", "oem probe",
		"oem logwrite" };

	for (unsigned i = 0; i < sizeof(blocked) / sizeof(blocked[0]); i++) {
		if (lc_starts(cmd, blocked[i])) {
			return 1;
		}
	}
	return 0;
}

static inline int
lc_busy(int hold, int fault, int live)
{
	return live || fault || hold;
}

static inline int
lc_cmd_denied(int hold, int fault, int live, const char *cmd)
{
	return lc_busy(hold, fault, live) && lc_blocked_cmd(cmd);
}

static inline int
lc_stop_action(int hold, int live)
{
	if (live) {
		return LC_STOP_NOTIFY;
	}
	return hold ? LC_STOP_HELD : LC_STOP_IDLE;
}

/* CPU_ON answers that prove the request itself was refused -- the core was not started by
 * it, and nothing of the kernel's is in flight. Everything else (accepted, already on,
 * pending, internal failure, not present, disabled, an unknown negative, a positive) leaves
 * the core's state unknown and keeps the hold (ruling 2026-10-25: only the whitelist). */
static inline int
lc_cpu_on_refused(int64_t status)
{
	return status == PSCI_E_NOT_SUPPORTED || status == PSCI_E_INVALID_PARAMS ||
	    status == PSCI_E_DENIED || status == PSCI_E_INVALID_ADDRESS;
}

/* the request and its answer, as the loader acts on them: from the request on the core may
 * already own memory and devices; only the whitelist above hands that back */
static int64_t
lc_cpu_on_issue(uint64_t mpidr, uint64_t entry, uint64_t arg)
{
	int64_t status;

	kernel_resources_held = 1;
	status = psci_call(PSCI_CPU_ON, mpidr, entry, arg);
	kernel_resources_held = lc_cpu_on_refused(status) ? 0 : 1;
	return status;
}

/* only an explicit off answers the question; ON, ON_PENDING, errors and NOT_SUPPORTED do not */
static inline int
lc_core_off(int64_t state)
{
	return state == PSCI_AFFINITY_OFF;
}
/* LC-END */

/* LC-WRITE-BEGIN -- extracted verbatim by tools/test-loader-lifecycle.py: the whole of
 * write_logs, so the host test counts the real side effects (partition query, block writes,
 * echo) through stubbed firmware calls. */
static void
write_logs(const char *klog, uint64_t klog_len)
{
	efi_block_io *bio;

	compose_logs(klog, klog_len);
	/* while memory and devices are held, or after a firmware fault, not even the firmware's
	 * partition query runs (find_log_partition walks BootServices); the staged text is what
	 * "upload" fetches. The active watch loop is the only exception, and the exception
	 * handler revokes it at its entry (LC-EXC). */
	bio = ((kernel_resources_held || firmware_fault) && !active_watch) ? NULL : find_log_partition();
	if (bio == NULL) {
		/*
		 * No log partition, or a kernel that was started and not proven done may still own the
		 * storage: writing through it then belongs to the held set. boot_a writes are the oem
		 * logwrite command's business alone (review F09 ruling b): QEMU and the automatic phone
		 * flushes land here, echoing the returned kernel log on the console instead. "upload"
		 * still stages the same text.
		 */
		if (klog != NULL) {
			print("\n== KERNEL LOG ==\n");
			for (uint64_t i = 0; i < klog_len; i++) {
				put_char(klog[i]);
			}
			print("\n== END OF LOG ==\n");
		}
		return;
	}
	uint64_t bs = bio->media->block_size;
	uint64_t len = (staged_len + 1 + bs - 1) / bs * bs;
	efi_status ws, rs = 0;
	int same = 0;

	/*
	 * Only through UEFI's own transfer list (see hc_take_back) -- that is the dance redwood needs
	 * because its kernel takes the UFS controller over. annibale's kernel boots from a ramdisk and
	 * never touches the UFS controller, and while the loader waits on USB no kernel runs at all, so
	 * UEFI's BlockIo owns the storage outright: write straight through it on annibale (k90-port).
	 */
	if (!hc_list_is_uefis() && !skip_ufs_arbitration) {
		if (hc_log_skips++ < 2) {
			hc_print_state("log not written");
			print("log: NOT WRITTEN to logdump: UEFI's transfer list is not the one in the controller\n");
		}
		compose_logs(klog, klog_len);
		return;
	}
	ws = bio->write_blocks(bio, bio->media->media_id, 0, len, out_buf);

	if (!EFI_ERROR(ws)) {
		bio->flush_blocks(bio);
		/*
		 * And read back: that the log is in the partition is what a run of the kernel's own UFS
		 * driver must leave possible (the controller is shared with this one). The line goes into
		 * the copy the host fetches over USB, not into the partition, which already has the rest.
		 */
		if (len <= sizeof(block_buffer)) {
			rs = bio->read_blocks(bio, bio->media->media_id, 0, len, block_buffer);
			same = !EFI_ERROR(rs) && memcmp(block_buffer, out_buf, len) == 0;
		}
	}
	print("log: %d bytes written to logdump (%x), read back %s (%x)\n", len, ws,
	    EFI_ERROR(ws) ? "not tried" : same ? "the same" : "DIFFERENT", rs);
	compose_logs(klog, klog_len);
}
/* LC-WRITE-END */


/* F09-PROBE-BEGIN -- host-extracted verbatim by /tmp/task19/probe_harness.c */
/*
 * Storage diagnostics. Review F09: entering the USB diagnostics used to write a 4 KiB probe
 * pattern over LBA 0 of five partitions -- misc and metadata are Android's live partitions and
 * boot_a carries the boot image on the slot-boot path -- and nothing was saved or restored.
 * The automatic path now only reads: inspect_one() and inspect_storage() report what BlockIo
 * exposes without a single write. A write check survives behind the explicit "oem probe"
 * command, and even there it saves the block first and writes it back, verifying every step.
 * (Host simulation: logs saved under /tmp/task19/.)
 */
static const char *
inspect_one(const char16 *name, unsigned len, const char *tag)
{
	efi_block_io *bio = find_partition_named(name, len);

	print("probe %s: handle %x\n", tag, (uint64_t)(uintptr_t)bio);
	if (bio == NULL) {
		return NULL;
	}
	print("probe %s: bs=%d blocks=%x mediaid=%x\n", tag, bio->media->block_size,
	    (uint64_t)bio->media->last_block, (uint64_t)bio->media->media_id);
	static char blk[4096];

	memset(blk, 0, sizeof(blk));
	efi_status r = bio->read_blocks(bio, bio->media->media_id, 0, sizeof(blk), blk);
	print("probe %s: read=%x head0=%x (read only, no write)\n", tag, r, *(uint32_t *)blk);
	return tag;
}

/*
 * The writable check, for one partition, non-destructively: the original first block goes into
 * memory first, the pattern is verified by read-back, and the original goes back and is itself
 * read back. Only the explicit "oem probe" command reaches this.
 */
static void
probe_one(const char16 *name, unsigned len, const char *tag)
{
	static char blk[4096], orig[4096];

	efi_block_io *bio = find_partition_named(name, len);

	print("probe %s: handle %x\n", tag, (uint64_t)(uintptr_t)bio);
	if (bio == NULL) {
		return;
	}
	print("probe %s: bs=%d blocks=%x mediaid=%x\n", tag, bio->media->block_size,
	    (uint64_t)bio->media->last_block, (uint64_t)bio->media->media_id);
	memset(orig, 0, sizeof(orig));
	memset(blk, 0, sizeof(blk));
	efi_status r0 = bio->read_blocks(bio, bio->media->media_id, 0, sizeof(orig), orig);
	if (EFI_ERROR(r0)) {
		print("probe %s: save read=%x, not touching the partition\n", tag, r0);
		return;
	}
	memset(blk, '.', sizeof(blk));
	memcpy(blk, "XNUPROBE-", 9);
	memcpy(blk + 9, tag, str_len(tag));
	memcpy(blk + 9 + str_len(tag), "-001\n", 5);
	efi_status w = bio->write_blocks(bio, bio->media->media_id, 0, sizeof(blk), blk);
	efi_status f = bio->flush_blocks(bio);
	memset(blk, 0, sizeof(blk));
	efi_status r = bio->read_blocks(bio, bio->media->media_id, 0, sizeof(blk), blk);
	int same = !EFI_ERROR(r) && memcmp(blk, "XNUPROBE-", 9) == 0;
	memcpy(blk, orig, sizeof(blk));
	efi_status w2 = bio->write_blocks(bio, bio->media->media_id, 0, sizeof(blk), blk);
	efi_status f2 = bio->flush_blocks(bio);
	memset(blk, 0, sizeof(blk));
	efi_status r2 = bio->read_blocks(bio, bio->media->media_id, 0, sizeof(blk), blk);
	int back = !EFI_ERROR(r2) && memcmp(blk, orig, sizeof(orig)) == 0;
	print("probe %s: save=%x write=%x flush=%x read=%x pattern=%s restore=%x/%x restored=%s\n", tag,
	    r0, w, f, r, same ? "seen" : "NOT seen", w2, f2, back ? "yes" : "NO -- CHECK THE PARTITION");
}

static const char16 n_boot_a[] = u"boot_a";
static const char16 n_misc[] = u"misc";
static const char16 n_metadata[] = u"metadata";
static const char16 n_logfs[] = u"logfs";
static const char16 n_sclogs[] = u"xbl_sc_logs";

static const struct {
	const char16 *name;
	unsigned len;
	const char *tag;
} probe_targets[] = {
	{ n_boot_a, sizeof(n_boot_a) / sizeof(char16), "boot_a" },
	{ n_misc, sizeof(n_misc) / sizeof(char16), "misc" },
	{ n_metadata, sizeof(n_metadata) / sizeof(char16), "metadata" },
	{ n_logfs, sizeof(n_logfs) / sizeof(char16), "logfs" },
	{ n_sclogs, sizeof(n_sclogs) / sizeof(char16), "sclogs" },
};

/* what the USB entry runs automatically: read-only, zero writes */
static void
inspect_storage(void)
{
	for (unsigned i = 0; i < sizeof(probe_targets) / sizeof(probe_targets[0]); i++) {
		inspect_one(probe_targets[i].name, probe_targets[i].len, probe_targets[i].tag);
	}
}

/* the explicit-write version, reached only through "oem probe" (kept under its old name) */
static void
probe_boot_a(void)
{
	for (unsigned i = 0; i < sizeof(probe_targets) / sizeof(probe_targets[0]); i++) {
		probe_one(probe_targets[i].name, probe_targets[i].len, probe_targets[i].tag);
	}
}
/* F09-PROBE-END */

static void
flush_log(void)
{
	write_logs(NULL, 0);
}

/*
 * Load a staged disk image off a real GPT partition into the resident root address, which is
 * where QEMU's host leaves it in guest RAM (docs/analysis/rootfs-strategy.md route D): the
 * 2.4 GB iOS system volume therefore needs neither the boot image nor a kernel storage driver.
 * EDK2 BlockIO reads work on this phone (writes do not, which this path never needs).
 */
static int
rdfile_load(const char *part, uint64_t off, uint64_t size, uint64_t dst)
{
	static uint8_t bounce[262144];   /* some UFS BlockIO drivers cap single transfers; 256 KiB is safe */
	char16 wname[40];
	unsigned n;

	for (n = 0; part[n] != 0 && n < sizeof(wname) / sizeof(wname[0]) - 1; n++) {
		wname[n] = part[n];
	}
	wname[n] = 0;
	efi_block_io *bio = find_partition_named(wname, n + 1);

	if (bio == NULL) {
		print("rdfile: partition %s not found\n", part);
		return 0;
	}
	uint64_t bs = bio->media->block_size;
	uint64_t total = (bio->media->last_block + 1) * bs;
	uint64_t start = off == ~0ull ? total - size : off;

	print("rdfile: %s bs=%d total=%x start=%x size=%x\n", part, (uint64_t)bs, total, start, size);
	if (size == 0 || (start % bs) != 0 || start + size > total) {
		print("rdfile: bad range\n");
		return 0;
	}
	memset((void *)dst, 0, size); /* image holes must read as zeroes, as QEMU's sparse file gives */
	for (uint64_t done = 0; done < size; ) {
		uint64_t chunk = size - done < sizeof(bounce) ? size - done : sizeof(bounce);

		efi_status s = bio->read_blocks(bio, bio->media->media_id, (start + done) / bs, chunk, bounce);
		if (EFI_ERROR(s)) {
			print("rdfile: read status %x at %x\n", s, done);
			return 0;
		}
		memcpy((void *)(dst + done), bounce, (size_t)chunk);
		done += chunk;
		if ((done >> 20) % 256 == 0) {
			print("rdfile: %x / %x\n", done, size);
		}
	}
	return 1;
}

/* ------------------------------------------------------------------------ */
/* Apple device tree writer                                                 */
/*                                                                          */
/* node := u32 nProperties, u32 nChildren, props[], children[]              */
/* prop := char name[32], u32 length, value padded to 4 bytes               */

struct dt_writer {
	uint8_t *base, *cur;
	uint32_t *counts[16]; /* [depth] -> {nProperties, nChildren} of the open node */
	int depth;
};

static void
dt_prop(struct dt_writer *w, const char *name, const void *data, uint32_t len)
{
	memset(w->cur, 0, 32);
	memcpy(w->cur, name, str_len(name));
	*(uint32_t *)(w->cur + 32) = len;
	memset(w->cur + 36, 0, (len + 3) & ~3u);
	memcpy(w->cur + 36, data, len);
	w->cur += 36 + ((len + 3) & ~3u);
	w->counts[w->depth - 1][0]++;
}

static void
dt_prop_str(struct dt_writer *w, const char *name, const char *value)
{
	dt_prop(w, name, value, (uint32_t)str_len(value) + 1);
}

static void
dt_prop_u32(struct dt_writer *w, const char *name, uint32_t value)
{
	dt_prop(w, name, &value, sizeof(value));
}

static void
dt_prop_u64(struct dt_writer *w, const char *name, uint64_t value)
{
	dt_prop(w, name, &value, sizeof(value));
}

/* Seconds since 1970 by the firmware's real-time clock, or 0 when it has none or it is not set. */
static uint64_t
firmware_epoch(void)
{
	static const uint16_t before_month[12] = { 0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334 };
	struct efi_time t;
	uint64_t days;

	if (ST->rt == NULL || ST->rt->get_time == NULL || EFI_ERROR(ST->rt->get_time(&t, NULL)) ||
	    t.year < 2024 || t.year > 2099 || t.month < 1 || t.month > 12) {
		return 0;
	}
	days = (uint64_t)(t.year - 1970) * 365 + (t.year - 1969) / 4 + before_month[t.month - 1] + (t.day - 1);
	if (t.month > 2 && t.year % 4 == 0) {
		days++;
	}
	uint64_t secs = days * 86400 + (uint64_t)t.hour * 3600 + (uint64_t)t.minute * 60 + t.second;
	if (t.timezone != 0x7FF) {
		secs += (int64_t)t.timezone * 60;       /* local time = UTC - timezone */
	}
	return secs;
}

/* A string in a field of fixed size, the rest zeros. */
static void
dt_prop_padded(struct dt_writer *w, const char *name, const char *value, uint32_t size)
{
	uint8_t field[64];

	memset(field, 0, sizeof(field));
	memcpy(field, value, str_len(value));
	dt_prop(w, name, field, size);
}

/*
 * Properties copied from the iPhone's device tree (identity.h), but one: the iPhone X's display is a
 * wide-gamut one ("artwork-display-gamut" P3), and with it iOS takes the screen for one that shows
 * extended colour (MobileGestalt's HasExtendedColorDisplay): WebKit then draws a page's tiles into
 * surfaces of ten bits a colour ('w30r', 'b3a8'), which neither the kernel's surfaces nor the window
 * server's renderer here do (eight bits, BGRA), and every page comes out black. Here the screen is
 * what the framebuffer is: sRGB.
 */
static int gamut_p3;    /* boot-arg gamut=p3 (a test): the iPhone's own "P3" left in place */

struct identity_table { const struct identity_prop *props; uint32_t count; const unsigned char *data; };
#define IDENTITY_TABLE(t) { t, sizeof(t) / sizeof(t[0]), t##_data }

/*
 * The iPhones the device tree can name, chosen by the payload's "identity" entry (mkpayload.py
 * --identity); without it the iPhone X, for iOS 16.5. chip-id and board-id are the ApChipID and
 * ApBoardID of the firmware's BuildManifest; the iPhone 17's device tree leaves platform-name empty
 * (iBoot fills it in), so it is named here after the chip.
 */
static const struct identity {
	const char *name, *platform;
	uint32_t chip_id, board_id;
	struct identity_table root, product, haptics, camera, buttons;
} identities[] = {
	{ "d22", "t8015", 0x8015, 0x06, IDENTITY_TABLE(identity_root), IDENTITY_TABLE(identity_product),
	  IDENTITY_TABLE(identity_haptics), IDENTITY_TABLE(identity_camera), { NULL, 0, NULL } },
	{ "v57", "t8150", 0x8150, 0x08, IDENTITY_TABLE(v57_identity_root), IDENTITY_TABLE(v57_identity_product),
	  IDENTITY_TABLE(v57_identity_haptics), IDENTITY_TABLE(v57_identity_camera), IDENTITY_TABLE(v57_identity_buttons) },
};

static void
dt_identity(struct dt_writer *w, const struct identity_table *t)
{
	for (uint32_t i = 0; i < t->count; i++) {
		if (str_eq(t->props[i].name, "artwork-display-gamut") && !gamut_p3) {
			dt_prop(w, t->props[i].name, "sRGB", 5);
			continue;
		}
		dt_prop(w, t->props[i].name, t->data + t->props[i].offset, t->props[i].length);
	}
}

/* One property of those copied from the iPhone's device tree, by name. */
static void
dt_identity_one(struct dt_writer *w, const struct identity_table *t, const char *name)
{
	for (uint32_t i = 0; i < t->count; i++) {
		if (str_eq(t->props[i].name, name)) {
			dt_prop(w, t->props[i].name, t->data + t->props[i].offset, t->props[i].length);
		}
	}
}

static void
dt_begin(struct dt_writer *w, const char *name)
{
	if (w->depth > 0) {
		w->counts[w->depth - 1][1]++;
	}
	w->counts[w->depth++] = (uint32_t *)w->cur;
	memset(w->cur, 0, 8);
	w->cur += 8;
	dt_prop_str(w, "name", name);
}

static void
dt_end(struct dt_writer *w)
{
	w->depth--;
}

