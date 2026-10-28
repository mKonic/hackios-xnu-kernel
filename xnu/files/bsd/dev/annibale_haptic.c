/*
 * The vibration motor of the REDMI K90 (annibale, SM8750 "sun"): a linear resonant actuator driven by
 * the haptics block of the PMIC -- TI/Qualcomm's "hv-haptics" (HAP), not a chip on I2C as under the
 * POCO X5 Pro (redwood, bsd/dev/redwood_haptic.c). It is reached over SPMI, like the power and volume
 * keys of redwood (bsd/dev/redwood_buttons.c), but through the version 7 register layout of the
 * arbiter, which Linux reads in drivers/spmi/spmi-pmic-arb.c and which nothing here had spoken to yet.
 *
 * Everything belonging to this phone is from its own device tree (the merged DT of docs/analysis,
 * node qcom,hv-haptics@f000) and from the vendor driver of a phone of this same platform,
 * qcom-hv-haptics.c of kernel_oneplus_sm8750 branch "sixteen" (8759 lines; mainline never took it).
 * Only the sequences a first bring-up needs are distilled here -- open-loop, like Android's own
 * initial state: no closed-loop calibration, no boost regulator control, no interrupts (polled):
 *
 *   SPMI       the arbiter of the first bus, spmi@c42d000: core@c400000 (its version, and which
 *              peripheral each channel is, from 0x2000), cnfg@c42d000 (who owns each channel),
 *              chnls@c500000 (write channels, 0x1000 each), obsrvr@c440000 (read channels: ours, the
 *              application processor's, system 0, at 0x8000 + 0x20 per channel). The haptics block
 *              answers as three SPMI peripherals of the PMIH010X at slave 7: 0x7f0 the driver
 *              (HAP), 0x7f1 its pattern engine, 0x7f2 its boost regulator -- the device tree's
 *              qcom,hv-haptics@f000, under qcom,pmih010x@7, with reg <0xf000 0xf100 0xf200>.
 *   the motor  5714 us as the device tree says (lra-period-us 0x1652: its period, open-loop written
 *              to the driver in steps of 5 us); 1300 mV (vmax-mv) in steps of 50 mV; the brake
 *              pattern ff 3f 1f over eight steps (qcom,brake-pattern), closed-loop brake mode 1,
 *              sine-shaped brake and drive waveforms (both sig-shape 1), sine gain 0.
 *   the motor for iOS (hv_haptic=4)  as for redwood: what iOS plays on its "Actuator" device
 *              (userland/src/audio_plugin.c) is written to /dev/redwood-haptic as samples of one
 *              channel; a waveform after a pause is a tap, one that goes on longer a vibration, the
 *              strength following how loud the samples are. But where the redwood chip plays
 *              Android's own RAM waveforms, this one is driven directly: its pattern engine takes a
 *              single amplitude (DIRECT_PLAY) and drives the motor at that strength, so the
 *              loudness of iOS's waveform becomes the amplitude, refreshed as iOS's goes up and down.
 *
 * Nothing here is done unless a boot-arg asks for it (the redwood motor's `haptic=1..5' is its own):
 *
 *   hv_haptic=1  a look, reading only: the arbiter's table, the three SPMI peripherals' revisions,
 *                the driver's fault and status registers as Android's driver leaves them.
 *   hv_haptic=2  ... the driver module enabled and configured (faults cleared, VMAX, the motor's
 *                period, brake, waveforms), every write read back; nothing played.
 *   hv_haptic=3  ... and one short direct drive for 30 ms at haptic_amp=N of 255 (0x60 by default):
 *                a blind test run says through the fault register whether the motor moved.
 *   hv_haptic=4  ... and the motor for iOS until the run ends, as redwood's level 5: /dev/redwood-haptic.
 *
 * Limits, whatever asks: a fault the driver reports (under-voltage, over-current, over-temperature,
 * the boost's short circuit), a write that does not read back, a channel nobody owns for us, and the
 * driver module is disabled and nothing more is done for the rest of the run; no vibration longer
 * than HAPTIC_MAX_MS, after which the motor rests for as long. At the run's end (and on a panic's
 * way out) the driver module is disabled. Nothing is written to the PMIC but this block's registers.
 */
#include <kern/clock.h>
#include <kern/locks.h>
#include <kern/thread.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <machine/machine_routines.h>
#include <pexpert/pexpert.h>
#include <miscfs/devfs/devfs.h>
#include <sys/conf.h>
#include <sys/systm.h>
#include <sys/uio.h>

/* the constants: this phone's device tree and the vendor driver, distilled; data only */
#include "annibale_haptic_data.h"

extern vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);
extern kern_return_t thread_terminate(thread_t thread);
extern void loader_log_putc(char c);

static vm_offset_t arb_core, arb_cnfg, arb_obsrvr;
static int level;
static uint32_t amplitude = 0x60;
static bool configured, playing, broken, leaving;
static bool written;                            /* something was written: the way out disables the module, configured or not */

/* every line to the loader's log as well (and to the console when that may be written) */
static int say(const char *format, ...) __printflike(1, 2);

static int
say(const char *format, ...)
{
	char line[300];
	va_list list;
	int length;

	va_start(list, format);
	length = vsnprintf(line, sizeof(line), format, list);
	va_end(list);
	for (const char *c = line; *c != 0; c++) {
		loader_log_putc(*c);
	}
	if (!leaving && ml_get_interrupts_enabled()) {
		printf("%s", line);
	}
	return length;
}

static inline uint32_t
rd(vm_offset_t address)
{
	return *(volatile uint32_t *)address;
}

static inline void
wr(vm_offset_t address, uint32_t value)
{
	*(volatile uint32_t *)address = value;
}

static void
spin_us(uint32_t us)
{
	uint64_t until;

	nanoseconds_to_absolutetime((uint64_t)us * NSEC_PER_USEC, &until);
	until += mach_absolute_time();
	while (mach_absolute_time() < until) {
	}
}

static void
wait_ms(uint32_t ms)
{
	if (leaving) {
		spin_us(ms * 1000);
	} else {
		delay_for_interval(ms, NSEC_PER_MSEC);
	}
}

static struct {
	uint16_t        ppid;                             /* slave << 8 | function: 0x7f0, 0x7f1, 0x7f2 -- slave 7 (PMIH010X #7), function 0xf0..0xf2 */
	uint16_t        apid;                             /* the channel it is reached through */
	bool            found, ours;                      /* and whether the system that may write is ours */
	vm_offset_t     channel;                          /* its write channel, mapped once known (648 to 650 on this phone) */
} pbuf[3] = { { HAP_DRIVER }, { HAP_PATTERN }, { HAP_BOOST } };

static bool
found_ppid(uint16_t ppid)
{
	for (unsigned i = 0; i < sizeof(pbuf) / sizeof(pbuf[0]); i++) {
		if (pbuf[i].ppid == ppid) {
			return pbuf[i].found;
		}
	}
	return false;
}

static LCK_GRP_DECLARE(spmi_lck_grp, "hv-haptic-spmi");
static LCK_SPIN_DECLARE(spmi_lock, &spmi_lck_grp);

/*
 * One transfer at a time through the arbiter (Linux holds one lock for the bus as well): a read goes
 * through the read window and is answered in it, a write through the write window, the address being
 * the 16 bits of the PMIC's and the channel that peripheral's. Returns NULL, or what went wrong.
 */
static const char *
spmi_read16(uint16_t ppid, uint16_t addr, uint8_t *bytes, uint32_t count)
{
	vm_offset_t channel;
	uint64_t start = mach_absolute_time(), now, interval;
	uint32_t status = 0, data[2] = { 0, 0 };
	const char *problem = NULL;
	int apid = -1;

	for (unsigned i = 0; i < sizeof(pbuf) / sizeof(pbuf[0]); i++) {
		if (pbuf[i].found && pbuf[i].ppid == ppid) {
			apid = pbuf[i].apid;
		}
	}
	if (apid < 0) {
		return "no channel is that peripheral";
	}
	nanoseconds_to_absolutetime(TRANSACT_TIMEOUT_US * (uint64_t)NSEC_PER_USEC, &interval);
	channel = arb_obsrvr + 0x8000UL * ARB_SYSTEM + 0x20UL * (uint32_t)apid;
	lck_spin_lock(&spmi_lock);
	wr(channel + CH_CMD, CMD_OPC(count > 4 ? OPC_EXT_READL : OPC_EXT_READ) | CMD_REG(addr & 0xff) | (count - 1));
	do {
		status = rd(channel + CH_STATUS);
		now = mach_absolute_time();
	} while (!(status & STATUS_DONE) && now - start < interval);
	if (status & STATUS_DONE) {
		data[0] = rd(channel + CH_RDATA0);
		data[1] = count > 4 ? rd(channel + CH_RDATA1) : 0;
	}
	lck_spin_unlock(&spmi_lock);

	if (!(status & STATUS_DONE)) {
		problem = "no answer from the arbiter";
	} else if (status & STATUS_DENIED) {
		problem = "the arbiter refuses";
	} else if (status & STATUS_FAILURE) {
		problem = "the transfer failed (nobody at that address?)";
	} else if (status & STATUS_DROPPED) {
		problem = "the transfer was dropped";
	} else {
		for (uint32_t i = 0; i < count; i++) {
			bytes[i] = (uint8_t)(data[i / 4] >> (8 * (i % 4)));
		}
	}
	return problem;
}

static const char *
spmi_write16(uint16_t ppid, uint16_t addr, const uint8_t *bytes, uint32_t count)
{
	vm_offset_t channel;
	uint64_t start = mach_absolute_time(), now, interval;
	uint32_t status = 0;
	const char *problem = NULL;
	int apid = -1;

	channel = 0;
	for (unsigned i = 0; i < sizeof(pbuf) / sizeof(pbuf[0]); i++) {
		if (pbuf[i].found && pbuf[i].ppid == ppid) {
			apid = pbuf[i].apid;
			channel = pbuf[i].channel;
			if (!pbuf[i].ours) {
				problem = "the arbiter lets another system write that peripheral";
			}
		}
	}
	if (apid < 0 && problem == NULL) {
		return "no channel is that peripheral";
	}
	if (problem != NULL) {
		return problem;
	}
	if (channel == 0) {
		return "the peripheral's write channel is not mapped";
	}
	nanoseconds_to_absolutetime(TRANSACT_TIMEOUT_US * (uint64_t)NSEC_PER_USEC, &interval);
	uint32_t w0 = 0, w1 = 0;

	for (uint32_t i = 0; i < count && i < 8; i++) {
		if (i < 4) {
			w0 |= (uint32_t)bytes[i] << (8 * i);
		} else {
			w1 |= (uint32_t)bytes[i] << (8 * (i - 4));
		}
	}
	lck_spin_lock(&spmi_lock);
	wr(channel + CH_WDATA0, w0);
	if (count > 4) {
		wr(channel + CH_WDATA1, w1);
	}
	/* Extended Register Write Long for every write: the short form left annibale_torch.c's register unchanged */
	wr(channel + CH_CMD, CMD_OPC(OPC_EXT_WRITEL) | CMD_REG(addr & 0xff) | (count - 1));
	do {
		status = rd(channel + CH_STATUS);
		now = mach_absolute_time();
	} while (!(status & STATUS_DONE) && now - start < interval);
	lck_spin_unlock(&spmi_lock);

	if (!(status & STATUS_DONE)) {
		problem = "no answer from the arbiter";
	} else if (status & STATUS_DENIED) {
		problem = "the arbiter refuses";
	} else if (status & STATUS_FAILURE) {
		problem = "the transfer failed";
	} else if (status & STATUS_DROPPED) {
		problem = "the transfer was dropped";
	}
	return problem;
}

/* one register, with the write read back and compared (the house rule) */
static bool
hap_read(uint16_t ppid, uint16_t reg, uint8_t *bytes, uint32_t count)
{
	const char *problem = spmi_read16(ppid, reg, bytes, count);

	if (problem != NULL) {
		say("haptic: reading %#06x gave: %s\n", reg, problem);
		broken = true;
		return false;
	}
	return true;
}

static bool
hap_put(uint16_t ppid, uint16_t reg, uint8_t value, bool quiet)
{
	uint8_t back = 0;

	if (spmi_write16(ppid, reg, &value, 1) != NULL || !hap_read(ppid, reg, &back, 1)) {
		say("haptic: writing %#06x = %#04x did not go through\n", reg, value);
		broken = true;
		return false;
	}
	if (back != value) {
		say("haptic: %#06x = %#04x read back %#04x\n", reg, value, back);
		broken = true;
		return false;
	}
	if (!quiet) {
		say("haptic: %#06x = %#04x\n", reg, value);
	}
	return true;
}

/* Which channel is each of the three peripherals, and who may write it (all of them, for all anyone
 * here asks, answer through one apid or a couple: Linux keeps the first whose system may write). */
static bool
find_peripherals(void)
{
	uint32_t version = rd(arb_core + ARB_VERSION), channels = rd(arb_core + ARB_FEATURES) & 0x7ff;

	say("haptic: the arbiter is version %#010x with %u channels\n", version, channels);
	if (version < ARB_VERSION_7) {
		say("haptic: this layout (v7) was not expected: nothing more is done\n");
		return false;
	}
	if (channels > APID_MAX) {
		channels = APID_MAX;
	}
	for (uint32_t n = 0; n < channels; n++) {
		uint32_t map = rd(arb_core + APID_MAP(n)), ppid;
		unsigned owner;

		if (map == 0) {
			continue;
		}
		ppid = (map >> 8) & 0xfff;
		for (unsigned i = 0; i < sizeof(pbuf) / sizeof(pbuf[0]); i++) {
			if ((uint16_t)ppid != pbuf[i].ppid) {
				continue;
			}
			owner = rd(arb_cnfg + APID_OWNER(n)) & 7;
			say("haptic: channel %u is haptics function %#x, written by system %u%s\n", n, ppid & 0xff, owner,
			    (map & (1u << 24)) ? ", which also gets its interrupts" : "");
			if (!pbuf[i].found || owner == ARB_SYSTEM) {
				pbuf[i].found = true;
				pbuf[i].apid = (uint16_t)n;
				pbuf[i].ours = owner == ARB_SYSTEM;
			}
		}
	}
	for (unsigned i = 0; i < sizeof(pbuf) / sizeof(pbuf[0]); i++) {
		if (!pbuf[i].found) {
			say("haptic: no channel of the arbiter is haptics function %#x\n", pbuf[i].ppid & 0xff);
		} else if (pbuf[i].ours) {
			/* (the whole 4 MB of write channels cannot be mapped this early in the boot: kmem_alloc fails) */
			pbuf[i].channel = ml_io_map(ARB_CHNLS_PHYS + ARB_CHANNEL_SIZE * pbuf[i].apid, ARB_CHANNEL_SIZE);
		}
	}
	return pbuf[0].found && pbuf[1].found;
}

static void
show_state(const char *why)
{
	uint8_t rev[2], fault = 0, rt = 0, hpwr = 0, imp[2] = { 0, 0 }, ready = 0;

	say("haptic: %s:\n", why);
	if (hap_read(HAP_DRIVER, HAP_CFG_REVISION, rev, 2)) {
		say("haptic:   the driver's revision %#02x.%#02x\n", rev[1], rev[0]);
	}
	if (hap_read(HAP_PATTERN, HAP_PTN_REVISION, rev, 2)) {
		say("haptic:   the pattern engine's revision %#02x.%#02x\n", rev[1], rev[0]);
	}
	if (found_ppid(HAP_BOOST) && hap_read(HAP_BOOST, 0, rev, 2)) {
		say("haptic:   the boost's revision %#02x.%#02x\n", rev[1], rev[0]);
	}
	hap_read(HAP_DRIVER, HAP_CFG_FAULT_STATUS, &fault, 1);
	hap_read(HAP_DRIVER, HAP_CFG_INT_RT_STS, &rt, 1);
	hap_read(HAP_DRIVER, HAP_CFG_HPWR_INTF, &hpwr, 1);
	hap_read(HAP_DRIVER, HAP_CFG_RT_LRA_IMPED, imp, 2);
	hap_read(HAP_PATTERN, HAP_PTN_FIFO_READY, &ready, 1);
	say("haptic:   fault %#02x, real-time status %#02x, hpwr %#02x, impedance %u mohm, fifo ready %#02x\n", fault, rt, hpwr,
	    (unsigned)imp[0] | (unsigned)imp[1] << 8, ready);
	if (fault != 0) {
		say("haptic:   (a fault is set: under-voltage, over-current, over-temperature, or the boost's -- see the vendor's bits at 0x0c)\n");
	}
}

static bool
module_enable(bool enable)
{
	uint8_t value = 0;

	if (!hap_read(HAP_DRIVER, HAP_CFG_EN_CTL, &value, 1)) {
		return false;
	}
	value = enable ? HAP_EN : 0;
	if (enable) {
		spin_us(100);                 /* (the vendor's: a moment before enabling, or the hardware's debounce blocks it) */
	}
	return hap_put(HAP_DRIVER, HAP_CFG_EN_CTL, (uint8_t)value, true);
}

static bool
configure(void)
{
	uint32_t tlra = LRA_PERIOD_US / 5;
	uint8_t two[2], eight[8], clear = HAP_FAULT_CLR_ALL, fault = 0;

	written = true;
	if (!module_enable(true)) {
		return false;
	}
	/* FAULT_CLR is a command whose bits clear themselves (it reads 0): the fault status tells instead */
	if (spmi_write16(HAP_DRIVER, HAP_CFG_FAULT_CLR, &clear, 1) != NULL || !hap_read(HAP_DRIVER, HAP_CFG_FAULT_STATUS, &fault, 1)) {
		say("haptic: clearing the faults did not go through\n");
		broken = true;
		return false;
	}
	if (fault != 0) {
		say("haptic: the fault status is %#04x after clearing\n", fault);
		broken = true;
		return false;
	}
	if (!hap_put(HAP_DRIVER, HAP_CFG_VMAX, VMAX_MV / 50, false)) {     /* 1300 mV as this phone's device tree says */
		return false;
	}
	two[0] = (uint8_t)(tlra >> 8);                    /* (the vendor writes the two bytes of 0x5c together, high first; */
	two[1] = (uint8_t)tlra;                           /* its [3:0] mask falls out of the value itself at 5714 us)      */
	if (spmi_write16(HAP_DRIVER, HAP_CFG_TLRA_HIGH & 0xff, two, 2) != NULL) {
		say("haptic: writing the motor's period %#x us gave a problem\n", LRA_PERIOD_US);
		broken = true;
		return false;
	}
	if (!hap_put(HAP_DRIVER, HAP_CFG_BRAKE_MODE, (1u << 6) | 0x01, false)) {  /* closed-loop brake, sine-shaped, gain 0 */
		return false;
	}
	memcpy(eight, brake_samples, sizeof(eight));
	if (spmi_write16(HAP_PATTERN, HAP_PTN_BRAKE_SAMPLES & 0xff, eight, 8) != NULL) {
		say("haptic: writing the brake pattern gave a problem\n");
		broken = true;
		return false;
	}
	if (!hap_put(HAP_DRIVER, HAP_CFG_DRV_WF_SEL, 0x10 | 0x01, false)) {       /* two's complement, sine drive */
		return false;
	}
	configured = true;
	say("haptic: the driver is configured: %u mV, %u us period, brake ff 3f 1f, sine waveforms, module enabled\n", VMAX_MV,
	    LRA_PERIOD_US);
	return true;
}

/* one direct drive of `ms' at `amp': what level 3 plays, and what a tap of level 4 becomes */
static bool
drive(uint8_t amp, uint32_t ms)
{
	uint8_t fault = 0;

	if (!hap_put(HAP_PATTERN, HAP_PTN_DIRECT_PLAY, amp, true) ||
	    !hap_put(HAP_DRIVER, HAP_CFG_SPMI_PLAY, HAP_PLAY | HAP_BRAKE_EN | HAP_SRC_DIRECT, true)) {
		return false;
	}
	playing = true;
	wait_ms(ms);
	if (!hap_put(HAP_DRIVER, HAP_CFG_SPMI_PLAY, HAP_SRC_DIRECT, true)) {
		return false;
	}
	playing = false;
	wait_ms(10);                                      /* (the brake does its work over some periods) */
	if (!hap_read(HAP_DRIVER, HAP_CFG_FAULT_STATUS, &fault, 1)) {
		return false;
	}
	if (fault != 0) {
		say("haptic: the fault register reads %#02x after driving at %#02x: giving up on the motor\n", fault, amp);
		broken = true;
		return false;
	}
	return true;
}

/* ---- hv_haptic=4: the motor for iOS (the scheme of redwood's level 5, onto a direct drive) -------- */

static volatile uint64_t heard_us, quiet_us, onset_us;
static volatile uint32_t heard_peak;
static uint64_t written_frames;
static unsigned taps, loops, capped;

static uint64_t
now_us(void)
{
	uint64_t ns;

	absolutetime_to_nanoseconds(mach_absolute_time(), &ns);
	return ns / NSEC_PER_USEC;
}

/* From the audio server's I/O thread: never waits. */
static int
haptic_write(__unused dev_t dev, struct uio *uio, __unused int ioflag)
{
	int16_t samples[256];
	int error = 0;

	while (uio_resid(uio) >= 2) {
		user_ssize_t bytes = uio_resid(uio);
		uint32_t peak = 0;

		if (bytes > (user_ssize_t)sizeof(samples)) {
			bytes = sizeof(samples);
		}
		bytes &= ~(user_ssize_t)1;
		if ((error = uiomove((caddr_t)samples, (int)bytes, uio)) != 0) {
			break;
		}
		for (unsigned i = 0; i < bytes / 2; i++) {
			uint32_t v = samples[i] < 0 ? (uint32_t)-(int32_t)samples[i] : (uint32_t)samples[i];

			peak = v > peak ? v : peak;
		}
		written_frames += (uint64_t)bytes / 2;
		if (peak > SILENT_BELOW) {
			uint64_t now = now_us();

			if (heard_us == 0 || quiet_us > heard_us || now - heard_us >= ACTIVE_US) {
				onset_us = now;
				heard_peak = 0;
			}
			heard_us = now;
			if (peak > heard_peak) {
				heard_peak = peak;
			}
		} else {
			quiet_us = now_us();
		}
	}
	return error;
}

static int
haptic_open(__unused dev_t dev, __unused int flags, __unused int type, __unused struct proc *p)
{
	return 0;
}

static const struct cdevsw haptic_cdevsw = {
	.d_open = haptic_open, .d_close = haptic_open, .d_read = eno_rdwrt, .d_write = haptic_write,
	.d_ioctl = eno_ioctl, .d_stop = eno_stop, .d_reset = eno_reset, .d_select = eno_select,
	.d_mmap = eno_mmap, .d_strategy = eno_strat, .d_type = 0
};

/*
 * How strong to drive with what iOS plays: the redwood driver's reasoning, onto an amplitude of 255 --
 * from a third of iOS's full scale at the haptic_amp of the boot-arg, down to five eighths of it for
 * the softest feedbacks, never above.
 */
static uint8_t
amplitude_for(uint32_t peak)
{
	uint32_t low = amplitude * 5 / 8, third = 32767 / 3;

	return (uint8_t)(peak >= third ? amplitude : low + (amplitude - low) * peak / third);
}

static void
serve(void)
{
	enum { IDLE, TAP, LOOP, REST } state = IDLE;
	uint64_t since = 0, rest_until = 0, last_look = 0, tap_onset = 0, followed = 0;
	uint8_t driving = 0;
	int major = cdevsw_add(-1, &haptic_cdevsw);

	if (major < 0 || devfs_make_node(makedev(major, 0), DEVFS_CHAR, UID_ROOT, GID_WHEEL, 0666, "redwood-haptic") == NULL) {
		say("haptic: /dev/redwood-haptic cannot be made\n");
		return;
	}
	say("haptic: THE MOTOR PLAYS WHAT IOS PLAYS ON ITS ACTUATOR (/dev/redwood-haptic) from here on, until the run ends\n");
	while (!broken) {
		uint64_t now = now_us();
		bool active = heard_us != 0 && now - heard_us < ACTIVE_US;
		uint32_t peak = heard_peak;
		uint8_t amp = amplitude_for(peak);

		switch (state) {
		case IDLE:
		case TAP:
			if (active && onset_us != tap_onset && now - onset_us >= SETTLE_US) {
				uint64_t began = onset_us;

				if (state == TAP && playing) {
					if (!hap_put(HAP_DRIVER, HAP_CFG_SPMI_PLAY, HAP_SRC_DIRECT, true)) {
						break;
					}
					playing = false;
					wait_ms(4);
				}
				if (!hap_put(HAP_PATTERN, HAP_PTN_DIRECT_PLAY, amp ? amp : 1, true) ||
				    !hap_put(HAP_DRIVER, HAP_CFG_SPMI_PLAY, HAP_PLAY | HAP_BRAKE_EN | HAP_SRC_DIRECT, true)) {
					break;
				}
				playing = true;
				driving = amp ? amp : 1;
				state = TAP;
				since = now;
				tap_onset = began;
				taps++;
				if (taps + loops <= TOLD_EVENTS) {
					say("haptic: tap %u: iOS's waveform peaks at %u of 32768: the motor driven directly at %#04x\n", taps,
					    peak, driving);
				}
				break;
			}
			if (state == IDLE) {
				break;
			}
			if (active && quiet_us < tap_onset && now - since > TAP_US) {
				state = LOOP;
				since = followed = now;
				loops++;
				heard_peak = 0;
				if (taps + loops <= TOLD_EVENTS) {
					say("haptic: vibration %u: iOS goes on playing (peak %u): the motor keeps being driven, %u ms at "
					    "most\n", loops, peak, HAPTIC_MAX_MS);
				}
			} else if (!active && now - since > TAP_US) {
				if (!hap_put(HAP_DRIVER, HAP_CFG_SPMI_PLAY, HAP_SRC_DIRECT, true)) {
					break;
				}
				playing = false;
				state = IDLE;
			}
			break;
		case LOOP:
			if (active && now - followed >= FOLLOW_US) {
				followed = now;
				heard_peak = 0;
				if (amp != 0 && (amp > driving + 8 || amp + 8 < driving)) {
					if (!hap_put(HAP_PATTERN, HAP_PTN_DIRECT_PLAY, amp, true)) {
						break;
					}
					driving = amp;
				}
			}
			if (!active || now - since > HAPTIC_MAX_MS * 1000ull) {
				bool cut = now - since > HAPTIC_MAX_MS * 1000ull;

				if (cut) {
					capped++;
					rest_until = now + HAPTIC_MAX_MS * 1000ull;
				}
				if (!hap_put(HAP_DRIVER, HAP_CFG_SPMI_PLAY, HAP_SRC_DIRECT, true)) {
					break;
				}
				playing = false;
				state = rest_until > now ? REST : IDLE;
				if (taps + loops <= TOLD_EVENTS + 1) {
					say("haptic: vibration %u: %s after %llu ms (last driven at %#04x)\n", loops,
					    cut ? "CUT at the limit" : "iOS stopped, and so does the motor", (now - since) / 1000, driving);
				}
			}
			break;
		case REST:
			if (now >= rest_until) {
				state = IDLE;
			}
			break;
		}
		if (state != IDLE && state != REST && now - last_look > 1000000) {
			uint8_t fault = 0;

			last_look = now;
			if (!hap_read(HAP_DRIVER, HAP_CFG_FAULT_STATUS, &fault, 1) || fault != 0) {
				if (!broken) {
					say("haptic: the fault register reads %#02x while playing: giving up on the motor\n", fault);
					broken = true;
				}
				break;
			}
		}
		wait_ms(4);
	}
	say("haptic: the motor is given up on (%u taps and %u vibrations played, %u cut at %u ms)\n", taps, loops, capped,
	    HAPTIC_MAX_MS);
}

static void
haptic_thread(__unused void *parameter, __unused wait_result_t result)
{
	arb_core = ml_io_map(ARB_CORE_PHYS, ARB_CORE_SIZE);
	arb_cnfg = ml_io_map(ARB_CNFG_PHYS, ARB_CNFG_SIZE);
	arb_obsrvr = ml_io_map(ARB_OBSRV_PHYS, ARB_OBSRV_SIZE);
	if (arb_core == 0 || arb_cnfg == 0 || arb_obsrvr == 0) {
		say("haptic: the arbiter's windows cannot be mapped: nothing is done\n");
		goto done;
	}
	say("haptic: level %u: %s\n", level, level == 1 ? "a look, reading only" : level == 2 ? "the driver configured, nothing played" :
	    level == 3 ? "one direct drive of 30 ms" : "the motor for iOS");
	if (!find_peripherals()) {
		goto done;
	}
	show_state("as found");
	if (level >= 2) {
		if (!configure()) {
			goto done;
		}
		show_state("after configuring");
	}
	if (level == 3 && !broken) {
		if (drive((uint8_t)amplitude, 30)) {
			say("haptic: drove the motor at %#04x for 30 ms with no fault reported\n", amplitude);
		}
		show_state("after driving");
	} else if (level == 4 && !broken) {
		serve();
	}
done:
	if (written) {
		static const uint8_t stop_play = HAP_SRC_DIRECT, module_off = 0;

		(void)spmi_write16(HAP_DRIVER, HAP_CFG_SPMI_PLAY, &stop_play, 1);
		(void)spmi_write16(HAP_DRIVER, HAP_CFG_EN_CTL, &module_off, 1);
	}
	say("haptic: done\n");
	thread_terminate(current_thread());
}

void
annibale_haptic_init(void)
{
	thread_t thread;

	if (!PE_parse_boot_argn("hv_haptic", &level, sizeof(level)) || level == 0) {
		return;
	}
	if (level > 4) {
		level = 4;
	}
	PE_parse_boot_argn("haptic_amp", &amplitude, sizeof(amplitude));
	if (amplitude > 0xff) {
		amplitude = 0x60;
	}
	if (kernel_thread_start(haptic_thread, NULL, &thread) == KERN_SUCCESS) {
		thread_deallocate(thread);
	}
}

/* At the run's end and on a panic's way out: the driver module disabled, the motor silent. */
void
annibale_haptic_quiesce(void)
{
	if (written) {
		static const uint8_t stop_play = HAP_SRC_DIRECT, module_off = 0;

		leaving = true;
		(void)spmi_write16(HAP_DRIVER, HAP_CFG_SPMI_PLAY, &stop_play, 1);
		(void)spmi_write16(HAP_DRIVER, HAP_CFG_EN_CTL, &module_off, 1);
		if (level == 4) {
			say("haptic: at the run's end: %u taps and %u vibrations played (%u cut at %u ms), from %llu frames iOS wrote; "
			    "the driver module is disabled\n", taps, loops, capped, HAPTIC_MAX_MS, written_frames);
		}
	}
}
