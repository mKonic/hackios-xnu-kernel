/*
 * The buttons of the REDMI K90 (annibale, SM8750 "sun") -- power, volume up, volume down -- as a
 * source of button records for /dev/redwood-input (sys/redwood_input.h). Only with boot-arg
 * buttons=1; every address below belongs to this phone. bsd/dev/redwood_buttons.c is the same
 * story for the POCO X5 Pro (redwood); with buttons=1 both start, and each stops at the first
 * step on the arbiter's version: that one knows version 5 of it, this one version 7.
 *
 * None of the three is a pin of the SoC. They are inputs of two of the power management chips,
 * exactly as under redwood, only newer silicon:
 *   power        KPDPWR_N of the power-on block ("PON") of the PMK8550, the always-on chip:
 *                qcom,pmk8550@0, pon_hlos@1300 (the merged device tree's line 19961), the
 *                pwrkey key of the gpio_keys node taking its interrupt <0x00 0x13 0x07 0x03>
 *                (line 9168): slave 0, function 0x13, so the peripheral is ppid 0x013.
 *   volume down  RESIN_N of the same block, interrupt <0x00 0x13 0x06 0x03> (line 9173).
 *   volume up    GPIO6 of the PM8550, pulled up; the key pulls it low: the gpio_keys node's
 *                vol-up entry says `gpios = <&pm8550_gpios 6 0>' (line 9164: phandle 0x68f is
 *                pinctrl@8800 of qcom,pm8550@1, line 21037 -- NOT a TLMM pin of the SoC), and
 *                its state key_vol_up-default says input-enable, bias-pull-up, power-source 1,
 *                function "normal" (line 21098). The pin driver gives each pad its own SPMI
 *                peripheral at reg + (pin-1)*0x100 (pinctrl-spmi-gpio.c, pmic_gpio_probe), so
 *                pad 6 lives at 0x8d00 of slave 1: ppid 0x18d.
 * The route to both chips is the SoC's PMIC arbiter, whose version 7 layout Linux reads in
 * drivers/spmi/spmi-pmic-arb.c and which bsd/dev/annibale_haptic.c first spoke to from here;
 * the windows and the channel arithmetic below are that file's.
 *
 * THIS DRIVER ONLY READS. Nothing is written to either chip, neither a register of a PMIC nor the
 * configuration of the pin: the PMICs make the phone's supplies, and a wrong write there can
 * switch one off. The firmware reads the same keys itself (volume down and volume up choose
 * between fastboot and recovery under it as under redwood) and has left both inputs as they are
 * needed; the driver checks that before it believes them, and where it finds something else it
 * says so and leaves that key alone. The only thing written is the command word that asks the
 * arbiter for a read.
 *
 * What a press is: the PON's "real time status" has a bit per key, set while the key is held
 * (after the block's own debouncing) -- of the split "generation 3" kind this is, whose HLOS half
 * (subtype 0x09) keeps RESIN at bit 6 and KPDPWR at bit 7 (qpnp-power-on.c, QPNP_PON_GEN3_*);
 * the GPIO's status register has the level of the pad, and the key is active low. A thread reads
 * the two registers 60 times a second, and a key has changed when two looks in a row say so. A
 * key that reads as held before it has ever been seen released is not believed (a pin that is not
 * what it was taken for must not become a volume key held down for the whole session). No
 * interrupt is used, as in redwood_buttons.c; what one would take is said at the end of that file.
 *
 * The mapping: the phone has what the iPhone X has, whose user space runs here -- a side button
 * and two volume keys, no home button. Power is the side button (REDWOOD_BUTTON_LOCK).
 *
 * The bring-up is made in steps, each said in the log before it is made. Boot-arg
 * buttons_hv_steps=N stops after the first N (buttons=1 asks for all five):
 *   1  map the arbiter and read its version             (the first access to that block)
 *   2  find the channels of the two peripherals         (reads of the arbiter's tables)
 *   3  ask each peripheral what it is                   (the first transfers on the bus)
 *   4  read how the keys' inputs are set up, and what they say now
 *   5  watch them                                       (the default)
 *
 * Register layouts: Linux drivers/spmi/spmi-pmic-arb.c (arbiter version 7), drivers/input/misc/
 * qpnp-power-on.c (the generation-3 power-on block), drivers/pinctrl/qcom/pinctrl-spmi-gpio.c
 * (the pad), and the phone's own device tree (spmi@c42d000, pon_hlos@1300, pinctrl@8800,
 * gpio_keys) -- the merged one under docs/analysis, line numbers as in its 2026-10-23 decompile.
 */
#include <kern/clock.h>
#include <kern/locks.h>
#include <kern/thread.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <machine/machine_routines.h>
#include <pexpert/pexpert.h>
#include <sys/redwood_input.h>
#include <sys/systm.h>

extern vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);
extern kern_return_t thread_terminate(thread_t thread);

/*
 * Every line goes to the loader's log directly as well: without -v the kernel's own messages
 * reach that log only at the end of a run (with the kernel's message buffer), and "xl log"
 * during a run is to show a press when it happens.
 */
extern void loader_log_putc(char c);

static bool leaving;

static int say(const char *format, ...) __printflike(1, 2);

static int
say(const char *format, ...)
{
	char line[400];
	va_list list;

	va_start(list, format);
	vsnprintf(line, sizeof(line), format, list);
	va_end(list);
	for (const char *c = line; *c != 0; c++) {
		loader_log_putc(*c);
	}
	if (!leaving && ml_get_interrupts_enabled()) {
		printf("%s", line);
	}
	return 0;
}
#define printf say

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

/* ---- the PMIC arbiter, version 7 (the windows and arithmetic of annibale_haptic.c) -------------- */

/* the windows of the first controller, the device tree's spmi@c42d000 (reg names as they are there) */
#define ARB_CORE_PHYS           0x0c400000UL    /* "core": the version, and which peripheral each channel is */
#define ARB_CORE_SIZE           0x003000UL
#define ARB_CNFG_PHYS           0x0c42d000UL    /* "cnfg": which system owns each channel */
#define ARB_CNFG_SIZE           0x004000UL
#define ARB_CHNLS_PHYS          0x0c500000UL    /* "chnls": the write channels, 0x1000 each (nothing here writes) */
#define ARB_CHNLS_SIZE          0x00010000UL    /* of its 4M: the channels one uses are far under */
#define ARB_OBSRV_PHYS          0x0c440000UL    /* "obsrvr": the read channels, 0x8000 per system, 0x20 per channel */
#define ARB_OBSRV_SIZE          0x0080000UL

#define ARB_VERSION             0x000
#define ARB_VERSION_7           0x70000000u     /* everything at or over this is "v7" to Linux's driver */
#define ARB_FEATURES            0x004           /* [10:0] the number of channels */
#define ARB_SYSTEM              0               /* who we are to the arbiter: the device tree's "qcom,ee" */
#define APID_MAX                ((ARB_CORE_SIZE - 0x2000) / 4)
#define APID_MAP(n)             (0x2000UL + 4 * (n))    /* core: [19:8] the peripheral (slave and function) */
#define APID_OWNER(n)           (4 * (n))               /* cnfg: [2:0] the system that may write through it */
#define APID_INTERRUPTS         (1u << 24)              /* map bit: the owner also gets the interrupts */
#define ARB_MAX_CHANNELS        APID_MAX

#define CH_CMD                  0x00
#define CH_STATUS               0x08
#define CH_RDATA0               0x18            /* the bytes read, the first in the lowest byte */
#define CH_RDATA1               0x1c
#define STATUS_DONE             0x1
#define STATUS_FAILURE          0x2
#define STATUS_DENIED           0x4
#define STATUS_DROPPED          0x8
#define CMD_OPC(opc)            ((uint32_t)(opc) << 27) /* the opcode [29:27], the register [11:4], the bytes - 1 [2:0] */
#define CMD_REG(reg)            (((uint32_t)(reg) & 0xff) << 4)
#define OPC_EXT_READL           1               /* eight bytes */
#define OPC_EXT_READ            13              /* up to four */
#define READ_TIMEOUT_US         2000            /* a read takes some tens of microseconds */

static vm_offset_t arb_core, arb_cnfg, arb_chnls, arb_obsrvr;

/*
 * One transfer at a time through the arbiter for this system, whoever asks: the battery reader
 * (bsd/dev/annibale_battery.c) and the flashlight (bsd/dev/annibale_torch.c) take it as
 * well, since like the buttons they each have their own channels and Linux holds one lock for
 * the whole bus (two at once and the arbiter drops one, as redwood_buttons.c learnt). Started as
 * "redwood-spmi" for redwood's v5 arbiter in that file; the annibale drivers share this one, and
 * annibale_haptic.c with its private lock should join it once the three are proven on hardware.
 */
static LCK_GRP_DECLARE(spmi_hv_lck_grp, "redwood-spmi-hv");
static LCK_SPIN_DECLARE(spmi_hv_lock, &spmi_hv_lck_grp);

void
annibale_spmi_lock(void)
{
	lck_spin_lock(&spmi_hv_lock);
}

void
annibale_spmi_unlock(void)
{
	lck_spin_unlock(&spmi_hv_lock);
}

struct peripheral {
	const char *    name;
	uint16_t        ppid;           /* slave << 8 | function: the device tree's, 0x013 and 0x18d */
	uint8_t         function;       /* the function byte, for when the table's slave byte disagrees */
	uint16_t        channel;        /* the apid it answers through */
	bool            found, exact;   /* and whether that entry is the device tree's ppid, byte for byte */
	unsigned        owner;          /* the system that may write through it */
};
static struct peripheral pon = { .name = "the PMK8550's power-on block", .ppid = 0x013, .function = 0x13 };
static struct peripheral pin = { .name = "GPIO6 of the PM8550", .ppid = 0x18d, .function = 0x8d };

/* How the reads have gone: said once a minute. */
static struct {
	uint64_t reads, failed, total_ns, longest_ns, last_ns;
} bus;

/*
 * Read `count` (1 to 8) registers of a peripheral, from register `reg` on, through the read
 * window of its channel (every system has one per peripheral, whoever owns it). Returns NULL, or
 * what went wrong.
 */
static const char *
spmi_read(const struct peripheral *p, uint8_t reg, uint8_t *bytes, uint32_t count)
{
	vm_offset_t channel = arb_obsrvr + 0x8000UL * ARB_SYSTEM + 0x20UL * p->channel;
	uint64_t start = mach_absolute_time(), now, interval, taken;
	uint32_t status = 0, data[2] = { 0, 0 };
	const char *problem = NULL;

	nanoseconds_to_absolutetime(READ_TIMEOUT_US * (uint64_t)NSEC_PER_USEC, &interval);
	annibale_spmi_lock();
	wr(channel + CH_CMD, CMD_OPC(count > 4 ? OPC_EXT_READL : OPC_EXT_READ) | CMD_REG(reg) | (count - 1));
	do {
		status = rd(channel + CH_STATUS);
		now = mach_absolute_time();
	} while (!(status & STATUS_DONE) && now - start < interval);
	if (status & STATUS_DONE) {
		data[0] = rd(channel + CH_RDATA0);
		data[1] = count > 4 ? rd(channel + CH_RDATA1) : 0;
	}
	annibale_spmi_unlock();

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
	absolutetime_to_nanoseconds(now - start, &taken);
	bus.reads++;
	bus.total_ns += taken;
	bus.last_ns = taken;
	if (taken > bus.longest_ns) {
		bus.longest_ns = taken;
	}
	if (problem != NULL) {
		bus.failed++;
	}
	return problem;
}

/*
 * Which channel is that peripheral's. Several can be: one per system that may write to it. As
 * Linux chooses: the first, unless a later one is ours to write through. The table's [19:8] is
 * the slave and the function together, and the device tree gives us both -- but the table's
 * slave byte is trusted only as far as it agrees: bsd/dev/annibale_haptic.c first wrote its
 * haptics ppids as 0xf0 without the slave byte the vendor's decode gives (the chip sits at
 * slave 7: 0x7f0, the device tree's line 41612; since corrected), and a table that disagreed
 * on some other peripheral would strand a driver that matched the full ppid exactly -- so an
 * entry with the right function byte is kept as a candidate too, named as what it claims to
 * be; step 3 asks the peripheral itself what it is, and a candidate that answers wrong is
 * left alone.
 */
static void
find_channel(struct peripheral *p, uint32_t channels)
{
	for (uint32_t n = 0; n < channels; n++) {
		uint32_t map = rd(arb_core + APID_MAP(n)), ppid;
		bool exact;
		unsigned owner;

		if (map == 0 || (((map >> 8) & 0xfff) & 0xff) != p->function) {
			continue;
		}
		ppid = (map >> 8) & 0xfff;
		exact = ppid == p->ppid;
		owner = rd(arb_cnfg + APID_OWNER(n)) & 7;
		printf("buttons: channel %u is %#x (%s%s); written by system %u%s\n", n, ppid, p->name,
		    exact ? "" : ", which is not the ppid the device tree gives", owner,
		    (map & APID_INTERRUPTS) ? ", which also gets its interrupts" : "");
		/* take the exact one; failing that prefer one ours to write; failing that the first */
		if (!p->found || (exact && !p->exact) ||
		    (exact == p->exact && owner == ARB_SYSTEM && p->owner != ARB_SYSTEM)) {
			p->found = true;
			p->exact = exact;
			p->owner = owner;
			p->channel = (uint16_t)n;
		}
	}
	if (!p->found) {
		printf("buttons: no channel of the arbiter is %s (slave %u, function %#x)\n", p->name, p->ppid >> 8, p->function);
	}
}

/* ---- the two peripherals ------------------------------------------------------------------------- */

/* every peripheral of these PMICs */
#define REG_TYPE                0x04
#define REG_SUBTYPE             0x05
#define REG_RT_STATUS           0x10            /* the inputs as they are now */
#define REG_INT_SET_TYPE        0x11            /* interrupts: a bit per input in each of these */
#define REG_INT_POLARITY_HIGH   0x12
#define REG_INT_POLARITY_LOW    0x13
#define REG_INT_ENABLED         0x15
#define REG_INT_LATCHED         0x18
#define REG_INT_PENDING         0x19

/* the power-on block, of the kind that is split in two ("generation 3"): this is its half for the operating system */
#define PON_TYPE                0x01
#define PON_SUBTYPE_HLOS        0x09            /* qpnp-power-on.c PON_GEN3_HLOS; 0x08 is the PBS's other half */
#define PON_KPDPWR              0x80            /* in the status and in the interrupt registers, qpnp-power-on.c 133-134 */
#define PON_RESIN               0x40

/* a GPIO of the low or medium voltage kind */
#define GPIO_TYPE               0x10
#define GPIO_SUBTYPE_LV         0x10            /* pinctrl-spmi-gpio.c 40-43: 0x10 to 0x13, one layout */
#define GPIO_SUBTYPE_MV_VIN3    0x13
#define GPIO_LEVEL              0x01            /* in the status, pinctrl-spmi-gpio.c PMIC_MPP_REG_RT_STS */
#define GPIO_REG_MODE           0x40            /* [1:0]: 0 input, 1 output, 2 both, 3 analog */
#define GPIO_REG_SUPPLY         0x41
#define GPIO_REG_PULL           0x42            /* [2:0]: 0 to 3 up, 4 down, 5 none (the device tree asks for up) */
#define GPIO_REG_ENABLE         0x46            /* [7] */

#define POLL_MS                 16
#define MINUTE_TICKS            (60 * 1000 / POLL_MS)

static struct button {
	const char *    name;
	uint32_t        record;         /* REDWOOD_BUTTON_* */
	bool            usable;         /* its input is what it was taken for */
	bool            live;           /* it has been seen released */
	bool            last;           /* the look before this one */
	bool            down;           /* as reported */
	uint64_t        since;          /* when it went down */
	uint32_t        presses;
} power = { .name = "power", .record = REDWOOD_BUTTON_LOCK },
    volume_up = { .name = "volume up", .record = REDWOOD_BUTTON_VOLUME_UP },
    volume_down = { .name = "volume down", .record = REDWOOD_BUTTON_VOLUME_DOWN };

/* "12.345 s (08:13:05.918 UTC)": since boot as in the log's own stamps, and as the daemons' lines have it */
static void
now_text(char *text, size_t size, uint64_t now)
{
	clock_sec_t seconds;
	clock_usec_t micro;
	uint64_t up;

	absolutetime_to_nanoseconds(now, &up);
	clock_get_calendar_microtime(&seconds, &micro);
	snprintf(text, size, "%llu.%03llu s (%02u:%02u:%02u.%03u UTC)", up / NSEC_PER_SEC, up / NSEC_PER_MSEC % 1000,
	    (unsigned)(seconds / 3600 % 24), (unsigned)(seconds / 60 % 60), (unsigned)(seconds % 60), (unsigned)(micro / 1000));
}

static void
button_look(struct button *b, bool pressed)
{
	uint64_t now = mach_absolute_time(), held;
	bool steady = pressed == b->last;
	char when[64];

	b->last = pressed;
	if (!b->usable || !steady) {
		return;
	}
	if (!b->live) {
		if (!pressed) {
			b->live = true;
			printf("buttons: %s is released now: believed from here on\n", b->name);
		}
		return;
	}
	if (pressed == b->down) {
		return;
	}
	b->down = pressed;
	now_text(when, sizeof(when), now);
	if (pressed) {
		b->since = now;
		b->presses++;
		printf("buttons: %s PRESSED at %s\n", b->name, when);
	} else {
		absolutetime_to_nanoseconds(now - b->since, &held);
		printf("buttons: %s released at %s, after %llu ms\n", b->name, when, held / NSEC_PER_MSEC);
	}
	redwood_touch_post(pressed ? REDWOOD_BUTTON_DOWN : REDWOOD_BUTTON_UP, b->record, 0, 0);
}

/* One look at both peripherals. */
static void
buttons_poll(void)
{
	static unsigned said;
	const char *problem;
	uint8_t status;

	if (power.usable || volume_down.usable) {
		problem = spmi_read(&pon, REG_RT_STATUS, &status, 1);
		if (problem == NULL) {
			button_look(&power, status & PON_KPDPWR);
			button_look(&volume_down, status & PON_RESIN);
		} else if (said < 8) {
			said++;
			printf("buttons: reading %s: %s\n", pon.name, problem);
		}
	}
	if (volume_up.usable) {
		problem = spmi_read(&pin, REG_RT_STATUS, &status, 1);
		if (problem == NULL) {
			button_look(&volume_up, !(status & GPIO_LEVEL));       /* the key is active low */
		} else if (said < 8) {
			said++;
			printf("buttons: reading %s: %s\n", pin.name, problem);
		}
	}
}

/* Step 3: is each peripheral what the registers above are those of? */
static bool
identify(const struct peripheral *p, uint8_t type, uint8_t subtype_from, uint8_t subtype_to)
{
	const char *problem;
	uint8_t is[2];

	if (!p->found) {
		return false;
	}
	printf("buttons: next: read the type of %s through channel %u (2 registers of ppid %#x)\n", p->name, p->channel, p->ppid);
	problem = spmi_read(p, REG_TYPE, is, 2);
	if (problem != NULL) {
		printf("buttons: %s: %s\n", p->name, problem);
		return false;
	}
	printf("buttons: %s says type %#x, subtype %#x (the read took %llu us)\n", p->name, is[0], is[1], bus.last_ns / 1000);
	if (is[0] != type || is[1] < subtype_from || is[1] > subtype_to) {
		printf("buttons: ... which is not what this driver knows (type %#x, subtype %#x to %#x): left alone\n", type,
		    subtype_from, subtype_to);
		return false;
	}
	return true;
}

/* Step 4 for the power-on block: nothing to check but what it says now; its interrupt setup goes to the log. */
static void
pon_inputs(void)
{
	const char *problem;
	uint8_t r[8], more[2];

	printf("buttons: next: read the status and the interrupt setup of %s (registers 0x1310 to 0x1319)\n", pon.name);
	problem = spmi_read(&pon, REG_RT_STATUS, r, 8);
	if (problem == NULL) {
		problem = spmi_read(&pon, REG_INT_LATCHED, more, 2);
	}
	if (problem != NULL) {
		printf("buttons: %s: %s\n", pon.name, problem);
		return;
	}
	printf("buttons: power-on block: status %#x (power %s, volume down %s); interrupts: edge %#x, high %#x, low %#x, "
	    "enabled %#x, latched %#x, pending %#x\n", r[0], (r[0] & PON_KPDPWR) ? "HELD" : "released",
	    (r[0] & PON_RESIN) ? "HELD" : "released", r[REG_INT_SET_TYPE - REG_RT_STATUS], r[REG_INT_POLARITY_HIGH - REG_RT_STATUS],
	    r[REG_INT_POLARITY_LOW - REG_RT_STATUS], r[REG_INT_ENABLED - REG_RT_STATUS], more[0], more[1]);
	power.usable = volume_down.usable = true;
	power.last = r[0] & PON_KPDPWR;
	volume_down.last = r[0] & PON_RESIN;
}

/* Step 4 for the pin: it is volume up only if it is an input with a pull-up, and switched on. */
static void
pin_input(void)
{
	const char *problem;
	uint8_t c[8], status = 0;
	unsigned mode, pull;

	printf("buttons: next: read the setup and the level of %s (registers 0x8d40 to 0x8d47, 0x8d10)\n", pin.name);
	problem = spmi_read(&pin, GPIO_REG_MODE, c, 8);
	if (problem == NULL) {
		problem = spmi_read(&pin, REG_RT_STATUS, &status, 1);
	}
	if (problem != NULL) {
		printf("buttons: %s: %s\n", pin.name, problem);
		return;
	}
	mode = c[0] & 3;
	pull = c[GPIO_REG_PULL - GPIO_REG_MODE] & 7;
	printf("buttons: volume up's pin: mode %u (%s), supply %u, pull %u (%s), %s; registers 0x40.. = %02x %02x %02x %02x %02x %02x "
	    "%02x %02x; level %s\n", mode, mode == 0 ? "input" : mode == 1 ? "output" : mode == 2 ? "input and output" : "analog",
	    c[GPIO_REG_SUPPLY - GPIO_REG_MODE] & 7, pull, pull < 4 ? "up" : pull == 4 ? "down" : "none",
	    (c[GPIO_REG_ENABLE - GPIO_REG_MODE] & 0x80) ? "enabled" : "DISABLED", c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7],
	    (status & GPIO_LEVEL) ? "high (released)" : "LOW (held)");
	if (mode != 0 || pull >= 4 || !(c[GPIO_REG_ENABLE - GPIO_REG_MODE] & 0x80)) {
		printf("buttons: volume up's pin is not an enabled input with a pull-up. Making it one is a write to the PMIC, "
		    "which this driver does not do: volume up is left alone\n");
		return;
	}
	volume_up.usable = true;
	volume_up.last = !(status & GPIO_LEVEL);
}

static void
say_summary(void)
{
	printf("buttons: so far %u presses of power, %u of volume up, %u of volume down; now%s%s%s%s; %llu reads of the bus, "
	    "%llu us each (the longest %llu), %llu failed\n", power.presses, volume_up.presses, volume_down.presses,
	    power.down ? " power" : "", volume_up.down ? " volume-up" : "", volume_down.down ? " volume-down" : "",
	    (power.down || volume_up.down || volume_down.down) ? " held" : " none held", bus.reads,
	    bus.reads ? bus.total_ns / bus.reads / 1000 : 0, bus.longest_ns / 1000, bus.failed);
}

static void
buttons_thread(void *parameter, __unused wait_result_t result)
{
	uint32_t steps = (uint32_t)(uintptr_t)parameter, version, channels;
	bool pon_known, pin_known;

	printf("buttons: step 1: map the PMIC arbiter (%#lx, %#lx, %#lx) and read its version\n", ARB_CORE_PHYS, ARB_CNFG_PHYS,
	    ARB_OBSRV_PHYS);
	arb_core = ml_io_map(ARB_CORE_PHYS, ARB_CORE_SIZE);
	arb_cnfg = ml_io_map(ARB_CNFG_PHYS, ARB_CNFG_SIZE);
	arb_chnls = ml_io_map(ARB_CHNLS_PHYS, ARB_CHNLS_SIZE);
	arb_obsrvr = ml_io_map(ARB_OBSRV_PHYS, ARB_OBSRV_SIZE);
	if (arb_core == 0 || arb_cnfg == 0 || arb_chnls == 0 || arb_obsrvr == 0) {
		printf("buttons: the arbiter's windows cannot be mapped: no buttons\n");
		goto out;
	}
	version = rd(arb_core + ARB_VERSION);
	channels = rd(arb_core + ARB_FEATURES) & 0x7ff;
	printf("buttons: PMIC arbiter version %#x, %u channels\n", version, channels);
	if (version < ARB_VERSION_7) {
		printf("buttons: this driver knows version 7 of the arbiter and up: no buttons\n");
		goto out;
	}
	if (channels == 0 || channels > ARB_MAX_CHANNELS) {
		channels = ARB_MAX_CHANNELS;
	}
	if (steps < 2) {
		goto out;
	}

	printf("buttons: step 2: look through the arbiter's table for the channels of the two peripherals\n");
	find_channel(&pon, channels);
	find_channel(&pin, channels);
	if (steps < 3) {
		goto out;
	}

	printf("buttons: step 3: the first transfers on the SPMI bus\n");
	pon_known = identify(&pon, PON_TYPE, PON_SUBTYPE_HLOS, PON_SUBTYPE_HLOS);
	pin_known = identify(&pin, GPIO_TYPE, GPIO_SUBTYPE_LV, GPIO_SUBTYPE_MV_VIN3);
	if (steps < 4) {
		goto out;
	}

	printf("buttons: step 4: how the keys' inputs are set up\n");
	if (pon_known) {
		pon_inputs();
	}
	if (pin_known) {
		pin_input();
	}
	if (!power.usable && !volume_up.usable) {
		printf("buttons: nothing that can be read: no buttons\n");
		goto out;
	}
	if (power.last || volume_down.last || volume_up.last) {
		printf("buttons: a key reads as held before anything was watched:%s%s%s. It is not reported until it has been "
		    "seen released\n", power.last ? " power" : "", volume_up.last ? " volume-up" : "",
		    volume_down.last ? " volume-down" : "");
	}
	/* (a key that is released now is believed at once) */
	power.live = power.usable && !power.last;
	volume_down.live = volume_down.usable && !volume_down.last;
	volume_up.live = volume_up.usable && !volume_up.last;
	if (steps < 5) {
		goto out;
	}

	printf("buttons: step 5: watching%s%s%s, every %u ms: power is the side button\n", power.usable ? " power" : "",
	    volume_up.usable ? " volume-up" : "", volume_down.usable ? " volume-down" : "", POLL_MS);
	for (unsigned tick = 1;; tick++) {
		buttons_poll();
		/* once early on as well: a short run is to say what the reads cost too */
		if (tick % MINUTE_TICKS == 0 || tick == 10 * 1000 / POLL_MS) {
			say_summary();
		}
		if (bus.failed >= 1000 && bus.failed * 2 > bus.reads) {
			printf("buttons: more than half of %llu reads have failed: giving up\n", bus.reads);
			break;
		}
		delay_for_interval(POLL_MS, NSEC_PER_MSEC);
	}
out:
	if (steps < 5) {
		printf("buttons: stopping here (buttons_hv_steps=%u)\n", steps);
	}
	thread_terminate(current_thread());
}

void
annibale_buttons_init(void)
{
	uint32_t enabled = 0, steps = 5;
	thread_t thread;

	/* only with buttons=1: a phone at hand, somebody to press; the redwood driver starts on the
	 * same boot-arg and stops itself at the arbiter's version, as this one does if it is not 7 */
	if (!PE_parse_boot_argn("buttons", &enabled, sizeof(enabled)) || enabled == 0) {
		return;
	}
	PE_parse_boot_argn("buttons_hv_steps", &steps, sizeof(steps));
	if (steps == 0) {
		return;
	}
	if (steps > 5) {
		steps = 5;
	}
	if (kernel_thread_start(buttons_thread, (void *)(uintptr_t)steps, &thread) == KERN_SUCCESS) {
		thread_deallocate(thread);
	}
}

/*
 * Interrupts would take what looking does not pay for (see the end of redwood_buttons.c: the
 * same reasoning holds here): the arbiter's periph_irq through the interrupt controller, the
 * channels' interrupt registers in the "chnls" window 0x1000 apart (v7 moved them from v5's
 * 0x10000), and -- writes to the PMIC -- a bit per key in the power-on block's and the pin's
 * "interrupt type", "polarity" and "enable set", which the firmware may well have left set for
 * its own reading of the keys: step 4 prints what is there. The latched status is cleared by a
 * write to the peripheral, which this read-only driver does not make; that is why it looks.
 */
