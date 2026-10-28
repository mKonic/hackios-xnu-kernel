/* annibale DTS: PMK8550 SID 0, SDAM 0x7100, restart@48 bits <1 7>.
 * MiCode annibale-w-oss qcom-reboot-reason.c writes bootloader reason 2 through
 * nvmem; nvmem/core.c preserves bit 0 and shifts the reason left by one.
 * Included by annibale_buttons.c to reuse its v7 APID lookup and locked reads.
 * A verified target remains set after loader_return; the host then asks the loader to reboot. */

extern uint64_t loader_stub_slot[4];
extern void loader_return(void);

static const char *
reboot_write_byte(vm_offset_t channel, uint8_t value)
{
	uint64_t start = mach_absolute_time(), interval;
	uint32_t status;

	nanoseconds_to_absolutetime(READ_TIMEOUT_US * (uint64_t)NSEC_PER_USEC, &interval);
	annibale_spmi_lock();
	wr(channel + 0x10, value); /* CH_WDATA0 */
	/* Extended Write Long, one byte at offset 0x48; matches annibale_torch.c. */
	wr(channel + CH_CMD, CMD_OPC(0) | CMD_REG(0x48));
	do {
		status = rd(channel + CH_STATUS);
	} while (!(status & STATUS_DONE) && mach_absolute_time() - start < interval);
	annibale_spmi_unlock();
	if (!(status & STATUS_DONE)) return "write timeout";
	if (status & STATUS_DENIED) return "write denied";
	if (status & STATUS_FAILURE) return "write failed";
	if (status & STATUS_DROPPED) return "write dropped";
	return NULL;
}

void
annibale_reboot_probe(void)
{
	uint32_t enabled = 0, cpus = 0, version, channels;
	uint8_t size = 0, original = 0, target, back = 0;
	vm_offset_t write_channel;
	struct peripheral sdam = { .name = "PMK8550 reboot SDAM", .ppid = 0x071, .function = 0x71 };
	const char *problem = NULL;
	static const char *const disabled[] = {
		"buttons", "battery", "hv_haptic", "torch", "torch_secs", "adreno8", "pcie", "cpufreq", "touchscreen", "adsp", "wifi"
	};

	if (!PE_parse_boot_argn("annibale_reboot_lk", &enabled, sizeof(enabled))) {
		return;
	}
	if (enabled != 1 || !redwood_platform_is_annibale()) {
		problem = "requires annibale hardware description and annibale_reboot_lk=1";
		goto out;
	}
	if (!loader_stub_slot[1]) {
		panic("reboot-lk: no loader return stub; no hardware accessed");
	}
	if (!PE_parse_boot_argn("cpus", &cpus, sizeof(cpus)) || cpus != 1) {
		problem = "requires cpus=1";
		goto out;
	}
	for (unsigned i = 0; i < sizeof(disabled) / sizeof(disabled[0]); i++) {
		uint32_t value = 0;
		if (PE_parse_boot_argn(disabled[i], &value, sizeof(value)) && value) {
			printf("reboot-lk: conflicting boot argument %s=%u\n", disabled[i], value);
			problem = "peripheral startup requested";
			goto out;
		}
	}
	printf("reboot-lk: probe PMK8550 SID 0 SDAM 0x7100, restart offset 0x48 bits 1..7\n");
	arb_core = ml_io_map(ARB_CORE_PHYS, ARB_CORE_SIZE);
	arb_cnfg = ml_io_map(ARB_CNFG_PHYS, ARB_CNFG_SIZE);
	arb_obsrvr = ml_io_map(ARB_OBSRV_PHYS, ARB_OBSRV_SIZE);
	if (!arb_core || !arb_cnfg || !arb_obsrvr) {
		problem = "arbiter mapping failed";
		goto out;
	}
	version = rd(arb_core + ARB_VERSION);
	channels = rd(arb_core + ARB_FEATURES) & 0x7ff;
	printf("reboot-lk: arbiter version=%#x channels=%u\n", version, channels);
	if (version < ARB_VERSION_7 || version >= 0x80000000u || !channels || channels > APID_MAX) {
		problem = "invalid v7 arbiter layout";
		goto out;
	}
	find_channel(&sdam, channels);
	if (!sdam.found || !sdam.exact || sdam.owner != ARB_SYSTEM) {
		problem = "no exact PPID 0x071 writable by EE 0";
		goto out;
	}
	write_channel = ml_io_map(ARB_CHNLS_PHYS + 0x1000UL * sdam.channel, 0x1000);
	if (!write_channel) {
		problem = "write channel mapping failed";
		goto out;
	}
	problem = spmi_read(&sdam, 0x44, &size, 1);
	if (problem) goto out;
	/* qcom-spmi-sdam.c defines capacity in 32-byte units starting at 0x40. */
	if (!size || 0x48 >= 0x40 + 32u * size) {
		problem = "restart byte outside SDAM capacity";
		goto out;
	}
	problem = spmi_read(&sdam, 0x48, &original, 1);
	if (problem) goto out;
	target = (original & 1u) | (2u << 1);
	printf("reboot-lk: apid=%u owner=%u capacity=%u original=%#x target=%#x\n",
	    sdam.channel, sdam.owner, size * 32u, original, target);
	problem = reboot_write_byte(write_channel, target);
	if (problem) goto out;
	problem = spmi_read(&sdam, 0x48, &back, 1);
	if (problem) goto out;
	if (back != target) {
		printf("reboot-lk: target readback=%#x expected=%#x\n", back, target);
		problem = "target readback mismatch";
		goto out;
	}
	printf("reboot-lk: prepared apid=%u owner=%u byte=%#x reason=2; returning to loader for host reboot\n",
	    sdam.channel, sdam.owner, back);
out:
	/* Failed transfers are not retried: the target field may have changed. */
	if (problem) {
		printf("reboot-lk: preparation failed: %s; returning to loader without reset\n", problem);
	}
	loader_return();
	panic("reboot-lk: loader_return returned");
}
