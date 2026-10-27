/* Sequential RAM uploads share the existing USB receive buffer and memory window.
 * A partial image is never published through resident_rd_size. */
static struct {
	uint64_t total, received;
	int failed;
} ramdisk_transfer;

static int
ramdisk_pending(void)
{
	return ramdisk_transfer.total != 0 &&
	    (ramdisk_transfer.failed || ramdisk_transfer.received != ramdisk_transfer.total);
}

static void
ramdisk_fail(void)
{
	if (ramdisk_pending()) {
		ramdisk_transfer.failed = 1;
		resident_rd_size = 0;
	}
}

static void
ramdisk_forget(void)
{
	memset(&ramdisk_transfer, 0, sizeof(ramdisk_transfer));
}

/* Accept one complete hexadecimal field; overflow must not wrap into a valid size. */
static int
ramdisk_hex(const char **cursor, uint64_t *value)
{
	const char *p = *cursor;
	uint64_t number = 0;
	unsigned digits = 0;

	while (*p && *p != ' ') {
		unsigned digit;
		if (*p >= '0' && *p <= '9') digit = (unsigned)(*p - '0');
		else if (*p >= 'a' && *p <= 'f') digit = (unsigned)(*p - 'a') + 10;
		else if (*p >= 'A' && *p <= 'F') digit = (unsigned)(*p - 'A') + 10;
		else return 0;
		if (++digits > 16 || number > (~0ULL - digit) / 16) return 0;
		number = number * 16 + digit;
		p++;
	}
	if (!digits) return 0;
	*cursor = p;
	*value = number;
	return 1;
}

static const char *
ramdisk_begin(const char *arguments)
{
	uint64_t total, window;
	struct payload_info pi;

	if (!ramdisk_hex(&arguments, &total) || *arguments++ != ' ' ||
	    !ramdisk_hex(&arguments, &window) || *arguments || !total ||
	    window > MAX_MEM_SIZE_LIMIT || (window & (BASE_ALIGN - 1)) ||
	    window <= RESIDENT_RD_OFFSET + RESIDENT_RD_RESERVE ||
	    total > window - RESIDENT_RD_OFFSET - RESIDENT_RD_RESERVE) {
		ramdisk_fail();
		return "invalid ramdisk total/window";
	}
	/* choose_window cannot resize an existing allocation. Require the declared window exactly. */
	if (win_size && win_size != window) {
		ramdisk_fail();
		return "window already allocated: reboot to resize";
	}
	memset(&pi, 0, sizeof(pi));
	pi.max_mem = window;
	if (!choose_window(&pi) || win_size != window || win_base > ~0ULL - win_size) {
		ramdisk_fail();
		return "requested memory window unavailable";
	}
	ramdisk_transfer.total = total;
	ramdisk_transfer.received = 0;
	ramdisk_transfer.failed = 0;
	resident_rd_size = 0;
	download_to_ramdisk = 0;
	return NULL;
}

static int
ramdisk_block_size(const char *argument, uint64_t *size)
{
	if (ramdisk_transfer.failed || !ramdisk_hex(&argument, size) || *argument ||
	    !*size || *size > 0xffffffffULL || *size > ramdisk_transfer.total - ramdisk_transfer.received) {
		ramdisk_fail();
		return 0;
	}
	return 1;
}

static void
ramdisk_block_done(uint64_t wanted, uint64_t received)
{
	if (ramdisk_transfer.failed || received != wanted ||
	    wanted > ramdisk_transfer.total - ramdisk_transfer.received) {
		ramdisk_fail();
		return;
	}
	ramdisk_transfer.received += wanted;
	if (ramdisk_transfer.received == ramdisk_transfer.total) {
		resident_rd_size = ramdisk_transfer.total;
	}
}

static char *
ramdisk_status_hex(char *out, uint64_t value)
{
	int started = 0;
	for (int shift = 60; shift >= 0; shift -= 4) {
		unsigned digit = (unsigned)(value >> shift) & 15;
		if (digit || started || shift == 0) {
			*out++ = "0123456789abcdef"[digit];
			started = 1;
		}
	}
	return out;
}

static void
ramdisk_status(void)
{
	char reply[60], *p = reply;
	const char *state = ramdisk_transfer.failed ? "failed" :
	    ramdisk_pending() ? "receiving" : resident_rd_size ? "ready" : "idle";
	uint64_t total = ramdisk_pending() ? ramdisk_transfer.total : resident_rd_size;
	uint64_t received = ramdisk_pending() ? ramdisk_transfer.received : resident_rd_size;

	/* All three sizes are bounded by MAX_MEM_SIZE_LIMIT (nine hex digits). */
	while (*state) *p++ = *state++;
	*p++ = ' ';
	p = ramdisk_status_hex(p, received);
	*p++ = ' ';
	p = ramdisk_status_hex(p, total);
	*p++ = ' ';
	p = ramdisk_status_hex(p, win_size);
	*p = 0;
	fb_reply("OKAY", reply);
}

/* Called after the shared held/fault/live command check. */
static int
ramdisk_command(const char *cmd)
{
	if (str_eq(cmd, "getvar:ramdisk-protocol")) {
		fb_reply("OKAY", "1");
	} else if (str_eq(cmd, "getvar:ramdisk-state")) {
		ramdisk_status();
	} else if (starts_with(cmd, "oem ramdisk begin")) {
		const char *error = cmd[17] == ' ' ? ramdisk_begin(cmd + 18) : "expected total and window";
		if (error) ramdisk_fail();
		fb_reply(error ? "FAIL" : "OKAY", error ? error : "");
	} else if (starts_with(cmd, "oem ramdisk none")) {
		ramdisk_forget();
		download_to_ramdisk = 0;
		resident_rd_size = 0;
		fb_reply("OKAY", "");
	} else if (ramdisk_pending() && (starts_with(cmd, "oem run") || starts_with(cmd, "oem selftest") ||
	    starts_with(cmd, "oem ramdisk"))) {
		fb_reply("FAIL", "ramdisk incomplete: cancel or begin again");
	} else {
		return 0;
	}
	return 1;
}
