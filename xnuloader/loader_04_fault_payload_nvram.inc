/* ------------------------------------------------------------------------ */
/* Hypervisor watchdog (Qualcomm "Haven" virtual watchdog, SMC interface as  */
/* used by Linux drivers/virt/haven/hh_virt_wdt.c). Armed for the whole stay */
/* in the loader and the kernel: any hang ends in a reset without a human.   */

#define VIRT_WDT_CONTROL   0x86000005u
#define VIRT_WDT_STATUS    0x86000006u
#define VIRT_WDT_PET       0x86000007u
#define VIRT_WDT_SET_TIME  0x86000008u
#define WDT_BITE_MS        30000u

static const struct platform *PLAT;
static int wdt_armed;

static uint64_t smc_x3;             /* the fourth argument register, for the one call that has one */

static uint64_t
smc_call(uint64_t id, uint64_t a1, uint64_t a2, uint64_t *r1, uint64_t *r2)
{
	register uint64_t x0 __asm__("x0") = id;
	register uint64_t x1 __asm__("x1") = a1;
	register uint64_t x2 __asm__("x2") = a2;
	register uint64_t x3 __asm__("x3") = smc_x3;

	__asm__ volatile ("smc #0" : "+r" (x0), "+r" (x1), "+r" (x2), "+r" (x3)
                : : "x4", "x5", "x6", "x7", "x8", "x9", "x10", "x11", "x12", "x13", "x14", "x15", "x16", "x17");
	if (r1 != NULL) {
		*r1 = x1;
	}
	if (r2 != NULL) {
		*r2 = x2;
	}
	return x0;
}

static void
wdt_pet(void)
{
	if (wdt_armed) {
		smc_call(VIRT_WDT_PET, 0, 0, NULL, NULL);
	}
}

/* Change the time to the bite, and start counting again. The hypervisor takes 16 bits of milliseconds. */
static void
wdt_set_ms(uint64_t ms)
{
	if (ms > 65000) {
		ms = 65000;
	}
	if (wdt_armed) {
		smc_call(VIRT_WDT_SET_TIME, ms, ms, NULL, NULL);
		smc_call(VIRT_WDT_PET, 0, 0, NULL, NULL);
	}
}

static void
wdt_arm(void)
{
	uint64_t ctl = 0, since = 0;

	if (PLAT != &platform_redwood) {
		return; /* SMC is not routed anywhere useful on QEMU virt */
	}
	uint64_t st = smc_call(VIRT_WDT_STATUS, 0, 0, &ctl, &since);
	uint64_t r_time = smc_call(VIRT_WDT_SET_TIME, WDT_BITE_MS, WDT_BITE_MS, NULL, NULL);
	uint64_t r_ctl = smc_call(VIRT_WDT_CONTROL, 3, 0, NULL, NULL);
	uint64_t r_pet = smc_call(VIRT_WDT_PET, 0, 0, NULL, NULL);
	print("watchdog: status ret=%x ctl=%x since_pet=%x; set_time=%x enable=%x pet=%x\n", st, ctl, since, r_time,
	    r_ctl, r_pet);
	wdt_armed = (r_pet == 0);
}

/* ------------------------------------------------------------------------ */
/* UEFI exception hook: put the fault into the log instead of on the screen  */

extern char __ImageBase;

/* LC-EXC-BEGIN -- extracted verbatim by tools/test-loader-lifecycle.py. */
static void EFIAPI
exception_handler(int64_t type, uint64_t *ctx)
{
	/* first act: revoke the watch exemption; a fault leaves no proof that the storage or
	 * the other core is in a state to take new requests. A held kernel may still own the
	 * interrupt controller, so this path masks before the log work below and records the
	 * state the release point would have handed back. */
	active_watch = 0;
	if (kernel_resources_held) {
		held_irq_mask();
		firmware_fault = 1;
		running_live = NULL;
	}
	print("\nUEFI EXCEPTION type=%d elr=%x esr=%x far=%x lr=%x sp=%x spsr=%x\n", (uint64_t)type, ctx[EXC_CTX_ELR],
	    ctx[EXC_CTX_ESR], ctx[EXC_CTX_FAR], ctx[EXC_CTX_LR], ctx[EXC_CTX_SP], ctx[EXC_CTX_SPSR]);
	print("loader image base %x (elr offset %x)\n", (uint64_t)(uintptr_t)&__ImageBase,
	    ctx[EXC_CTX_ELR] - (uint64_t)(uintptr_t)&__ImageBase);
	if (PLAT == &platform_annibale && screen_quiet) {
		/*
		 * Preserve the fault for USB retrieval; a new kernel needs a host-requested reboot.
		 * This branch is the fault state itself, so it records one before it starts serving.
		 * The staged text is assembled here, not written: compose_logs touches no storage.
		 * The serve window is bounded, so a host that never comes back cannot keep this CPU
		 * here for ever; when it runs out the fault stays recorded and the CPU ends masked.
		 */
		firmware_fault = 1;
		running_live = NULL;
		set_result("firmware-fault", (uint64_t)type, ctx[EXC_CTX_ELR]);
		compose_logs(NULL, 0);
		uint64_t freq = READ_SYSREG(CNTFRQ_EL0), began = READ_SYSREG(CNTVCT_EL0);

		if (freq == 0) {
			/* Without a counter there is nothing to bound the service with: no round count
			 * stands in for the missing clock (its wall time would be a guess), so the fault
			 * is recorded and staged and this CPU goes straight to the reset attempt. */
			print("no counter frequency: the USB fault window cannot be bounded, not served\n");
		} else {
			uint64_t served = 0;

			while (READ_SYSREG(CNTVCT_EL0) - began <= FAULT_SERVE_SECONDS * freq) {
				usb_poll_command();
				served++;
			}
			print("USB fault service window closed after %d rounds: memory and devices stay held\n", served);
		}
	}
	if (kernel_resources_held || firmware_fault) {
		/* the same rule as reboot: no flush, no UEFI reset callback while the kernel may own
		 * the storage; a return keeps the fault recorded and ends this CPU, masked */
		int64_t status = psci_reset_attempt();

		print("PSCI SYSTEM_RESET returned %d: the fault stays recorded, memory and devices stay held\n",
		    (uint64_t)status);
		set_result("reset-failed", (uint64_t)status, 0);
	} else {
		/* nothing was handed over: the original cold-reset path, log flush included */
		flush_log();
		ST->rt->reset_system(0 /* EfiResetCold */, 0, 0, NULL);
	}
	__asm__ volatile ("msr daifset, #0xf");
	for (;;) {
	}
}
/* LC-EXC-END */

static void
install_exception_hook(void)
{
	static const efi_guid cpu_guid = EFI_CPU_ARCH_GUID;
	efi_cpu_arch *cpu;

	if (EFI_ERROR(BS->locate_protocol(&cpu_guid, NULL, (void **)&cpu))) {
		return;
	}
	for (int64_t type = 0; type <= 3; type += 3) { /* synchronous, SError */
		cpu->register_interrupt_handler(cpu, type, NULL);
		efi_status s = cpu->register_interrupt_handler(cpu, type, exception_handler);
		if (EFI_ERROR(s)) {
			print("exception hook %d: status %x\n", (uint64_t)type, s);
		}
	}
}

/* ------------------------------------------------------------------------ */
/* Payload                                                                  */

struct payload_info {
	const uint8_t *kernel;
	const uint8_t *ramdisk;
	uint32_t ramdisk_size;
	const char *cmdline;
	uint64_t checkpoint;
	uint64_t forced_base;
	uint64_t max_mem;               /* "memsize": the window's largest size, if not MAX_MEM_SIZE */
	const char *identity;           /* "identity": which iPhone iOS is told it runs on (identities[]) */
	uint64_t resident_rd;           /* size of a root image the host put at the resident address */
	char rdfile_part[40];           /* a staged system image to read from a real GPT partition (k90-port) */
	uint64_t rdfile_size;
	uint64_t rdfile_off;            /* from the partition start; ~0 means aligned to its end */
	int usb_mode;
};

static uint64_t
parse_number(const char *c, unsigned base)
{
	uint64_t v = 0;
	for (; *c; c++) {
		uint64_t d = (*c >= '0' && *c <= '9') ? (uint64_t)(*c - '0') : (*c >= 'a' && *c <= 'f') ? (uint64_t)(*c - 'a' + 10) : 16;
		if (d >= base) {
			break;
		}
		v = v * base + d;
	}
	return v;
}

/*
 * Actual bytes the last normal (payload-area) download received. A transfer that ends
 * early or is cancelled writes this on the way out, so the header it left behind cannot
 * be parsed until a complete container replaces it; 0 means no such download this boot.
 */
static uint64_t payload_bytes;

static int
nul_within(const char *s, uint32_t len)
{
	for (uint32_t i = 0; i < len; i++) {
		if (s[i] == 0) {
			return 1;
		}
	}
	return 0;
}

static int
parse_payload(struct payload_info *pi, int verbose)
{
	struct payload_header *ph = (struct payload_header *)PAYLOAD_BASE;
	int bad = 0, ended = 0;   /* ended: an all-zero record landed exactly at the body end */

	memset(pi, 0, sizeof(*pi));
	pi->cmdline = "";
	if (ph->magic != PAYLOAD_MAGIC) {
		print("no payload at %x (magic %x)\n", PAYLOAD_BASE, (uint64_t)ph->magic);
		return 0;
	}
	/*
	 * A magic match alone is not consumable: the declared body must fit inside the
	 * container bound (review U1), and once the loader has downloaded anything, the
	 * recorded real byte count must equal the declared file exactly: a smaller
	 * record marks a transfer that stopped on the way, a larger one leaves content
	 * past the declared end. Images preloaded by QEMU or the shim never went
	 * through a download, so the recorded-count gate only guards downloads.
	 */
	if (ph->size > PAYLOAD_CONTAINER_MAX - sizeof(struct payload_header)
	    || (payload_bytes != 0 && payload_bytes != sizeof(struct payload_header) + ph->size)) {
		print("payload size %x is not a container received in full (body bound %x)\n",
		      (uint64_t)ph->size, (uint64_t)(PAYLOAD_CONTAINER_MAX - sizeof(struct payload_header)));
		return 0;
	}
	const uint8_t *p = (const uint8_t *)(ph + 1), *pend = p + ph->size;
	while (p + sizeof(struct payload_entry) <= pend) {
		const struct payload_entry *e = (const struct payload_entry *)p;
		const char *text = (const char *)(p + sizeof(*e));
		uint64_t pad = ((uint64_t)e->size + 15) & ~15ull;

		if (e->name[0] == 0) {
			/*
			 * The body ends only at a record that is all zero for its whole 24
			 * bytes and whose last byte is the body's last byte: a partial tail,
			 * a nonzero terminator byte, or bytes past the terminator leave the
			 * consumer without proof of where the container stopped.
			 */
			const uint8_t *raw = (const uint8_t *)e;

			ended = p + sizeof(*e) == pend;
			for (unsigned z = 0; z < sizeof(*e); z++) {
				if (raw[z] != 0) {
					ended = 0;
				}
			}
			bad = !ended;
			break;
		}
		/* entry data stays inside the declared body and the name gets its NUL
		 * inside the 16 name bytes, or no consumer may read any of it */
		if (p + sizeof(*e) + pad > pend || !nul_within(e->name, sizeof(e->name))) {
			bad = 1;
			break;
		}
		if (verbose) {
			print("payload entry '%s' size %x\n", e->name, (uint64_t)e->size);
		}
		if (str_eq(e->name, "ramdisk")) {
			pi->ramdisk = p + sizeof(*e);             /* becomes /dev/md0, the root device */
			pi->ramdisk_size = e->size;
		} else if (str_eq(e->name, "kernel")) {
			pi->kernel = p + sizeof(*e);
		} else if (str_eq(e->name, "cmdline")) {
			if (!nul_within(text, e->size)) {
				bad = 1;
				break;
			}
			pi->cmdline = text;
		} else if (str_eq(e->name, "physbase")) {
			if (!nul_within(text, e->size)) {
				bad = 1;
				break;
			}
			pi->forced_base = parse_number(text, 16); /* testing aid; must be free RAM */
		} else if (str_eq(e->name, "memsize")) {
			if (!nul_within(text, e->size)) {
				bad = 1;
				break;
			}
			pi->max_mem = parse_number(text, 16) & ~(BASE_ALIGN - 1);
			if (pi->max_mem > MAX_MEM_SIZE_LIMIT) {
				bad = 1;
				break;
			}
		} else if (str_eq(e->name, "identity")) {
			if (!nul_within(text, e->size)) {
				bad = 1;
				break;
			}
			pi->identity = text;
		} else if (str_eq(e->name, "rdres")) {
			if (!nul_within(text, e->size)) {
				bad = 1;
				break;
			}
			pi->resident_rd = parse_number(text, 16); /* e.g. loaded there by QEMU; see RESIDENT_RD_OFFSET */
		} else if (str_eq(e->name, "checkpoint")) {
			if (!nul_within(text, e->size)) {
				bad = 1;
				break;
			}
			pi->checkpoint = parse_number(text, 10);  /* where the kernel should stop and come back */
		} else if (str_eq(e->name, "rdfile")) {
			if (!nul_within(text, e->size)) {
				bad = 1;
				break;
			}
			/*
			 * "<partition>:<bytes>[:<offset>|tail]": the resident root disk is staged on a
			 * real GPT partition instead of being left in RAM by the host (k90-port; the
			 * QEMU milestone preloads it into guest RAM, docs/analysis/rootfs-strategy.md).
			 */
			const char *q = text;
			unsigned n = 0;

			while (*q && *q != ':' && n < sizeof(pi->rdfile_part) - 1) {
				pi->rdfile_part[n++] = *q++;
			}
			pi->rdfile_part[n] = 0;
			if (*q == ':') {
				pi->rdfile_size = parse_number(++q, 10);
				while (*q && *q != ':') {
					q++;
				}
				pi->rdfile_off = ~0ull;
				if (*q == ':' && !str_eq(++q, "tail")) {
					pi->rdfile_off = parse_number(q, 10);
				}
				pi->resident_rd = pi->rdfile_size;    /* same delivery path as QEMU's rdres */
			}
		} else if (str_eq(e->name, "usb")) {
			pi->usb_mode = 1;                         /* stay in the loader and take commands over USB */
		}
		p += sizeof(*e) + pad;
	}
	if (!bad && !ended) {
		bad = 1;   /* the entries ran to the body end with no terminator record */
	}
	if (bad) {
		print("payload container lost its entry bound, name NUL, or end terminator\n");
		memset(pi, 0, sizeof(*pi));
		pi->cmdline = "";
		return 0;
	}
	return 1;
}

/* ------------------------------------------------------------------------ */
/* NVRAM image                                                              */
/*                                                                          */
/* iBoot hands the kernel a copy of the NVRAM contents in CHRP format       */
/* (iokit/Kernel/IONVRAMCHRPHandler.cpp). There is no NVRAM here, so build  */
/* an image with a valid header and one empty "common" partition.           */

static void
nvram_chrp_header(uint8_t *h, uint8_t sig, uint32_t bytes, const char *name)
{
	uint16_t blocks = (uint16_t)(bytes / 16), sum;

	memset(h, 0, 16);
	h[0] = sig;
	h[2] = (uint8_t)blocks;
	h[3] = (uint8_t)(blocks >> 8);
	memcpy(h + 4, name, str_len(name));
	/* checksum over signature, length and name, folded to 8 bits */
	sum = h[0];
	for (int i = 2; i < 16; i++) {
		sum += h[i];
	}
	while (sum > 0xff) {
		sum = (sum & 0xff) + (sum >> 8);
	}
	h[1] = (uint8_t)sum;
}

static void
build_nvram_image(uint8_t *image)
{
	uint32_t lo = 1, hi = 0, generation = 1;

	memset(image, 0, NVRAM_BANK_SIZE);
	nvram_chrp_header(image, 0x5a, 0x20, "nvram");                           /* Apple header: 32 bytes */
	nvram_chrp_header(image + 0x20, 0x70, NVRAM_BANK_SIZE - 0x20, "common"); /* the rest: common partition */
	memcpy(image + 20, &generation, 4);
	/* Adler-32 over everything after the checksum field itself */
	for (uint32_t i = 20; i < NVRAM_BANK_SIZE; i++) {
		lo = (lo + image[i]) % 65521;
		hi = (hi + lo) % 65521;
	}
	uint32_t adler = (hi << 16) | lo;
	memcpy(image + 16, &adler, 4);
}

