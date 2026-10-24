/*
 * The flashlight of the REDMI K90 (annibale, SM8750 "sun"): the flash LED module of the PM8550
 * (SPMI slave 1, registers 0xee00; the device tree's qcom,flash_led@ee00 under qcom,pm8550@1,
 * line 20878 of the merged decompile, "qcom,qti-pm8350c-flash-led" -- four channels, the same
 * block family as redwood's PM8350C of bsd/dev/redwood_torch.c), with the rear flash LED on its
 * channel 0. The camera's torch and flash hang off it: the camera nodes name pm8550_torch0
 * (phandle 0x6b7, line 20945: torch, current step 12500 uA, max-current-ma 0x1f4 -- 500) and
 * pm8550_flash0 (0x6b3, line 20988: flash, same step, 500 mA for 300 ms).
 *
 * Numbers come from the vendor driver of this platform, drivers/leds/leds-qti-flash.c of
 * kernel_oneplus_sm8750 branch "sixteen" (the device tree's compatible names it), from the
 * device tree itself, and from what bsd/dev/redwood_torch.c verified register by register on
 * redwood's same-generation module: the module answers type 0x18 and subtype 0x07 at 0x04 and
 * 0x05; the torch current of Android's flashlight there was 125 mA in steps of 12.5 mA --
 * ITARGET code round(mA / 12.5) - 1 at IRESOLUTION bit 0 clear -- the module enabled by
 * FLASH_ENABLE_CONTROL = 0x80, then the channel by EN_LED_CTRL bit 0, and off in the reverse
 * order with ITARGET back to 0. The vendor driver lights a torch exactly so and, for a torch
 * only, deliberately leaves the per-channel safety timer disarmed (its flash path arms it, its
 * disable path clears its bit: leds-qti-flash.c 457-460 and 497-500): the torch is guarded in
 * software, ten times a second, on the module's own fault flags -- which is what this file does
 * too, as redwood_torch.c does.
 *
 * Writing to a PMIC is not like writing anywhere else: the PMICs make the phone's supplies. So
 * every write goes through one function that takes only this module's registers, every write is
 * read back, and any surprise ends all writing for the run; nothing at all is written unless the
 * module answered as the module, with its strobe already the software's as Android leaves it and
 * the module dark as it should be after a boot. A fault the module reports in real time while
 * nothing is lit stops the run's lighting: the registers that would clear a latched fault have
 * no write semantics in the vendor driver (it clears them by regmap through 0x18, whose effect
 * on latched status is implied and undocumented here), so this driver does not guess -- TODO
 * after the first look at a real one. Transfers go one at a time through the arbiter with the
 * buttons' and battery's readers (annibale_spmi_lock()).
 *
 * Nothing here is done unless a boot-arg asks for it:
 *
 *   torch_secs=N   the blind test: channel 0 lights at torch_ma (125 by default, Android's
 *                  torch current) for N seconds -- clamped to 1 to 5 -- then goes dark again,
 *                  every write read back. This also brings the module up for the primitives
 *                  below; without it they bring it up themselves when first asked.
 *   torch_ma=M     the current of the test and of later light, 1 to 500 (the device tree's
 *                  torch0 max-current-ma, line 20946).
 *
 * Two primitives are kept for the camera (task L1b: the dylib's FigFlashlight, which on redwood
 * is served through /dev/redwood-torch at level 0 to 1000): annibale_torch_low_on(mA) lights
 * channel 0 at up to the torch maximum and annibale_torch_low_off() puts it out; both fail
 * quietly once the module has said anything surprising. At the run's end (and on a panic's way
 * out) the light goes off.
 *
 * Register layouts: Linux drivers/spmi/spmi-pmic-arb.c (arbiter version 7: annibale_haptic.c's
 * windows), qcom's leds-qti-flash.c of this platform, and redwood_torch.c's verification of the
 * same module one phone older.
 */
#include <kern/clock.h>
#include <kern/locks.h>
#include <kern/thread.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <machine/machine_routines.h>
#include <pexpert/pexpert.h>
#include <sys/redwood_audio.h>
#include <sys/redwood_input.h>
#include <sys/systm.h>

extern vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);
extern kern_return_t thread_terminate(thread_t thread);
extern void loader_log_putc(char c);

static uint32_t secs, milliamps = 125;
static bool configured, lit, broken, leaving, timer_cleared;
static uint8_t timer_original;                  /* channel 0's safety timer as found, restored on the way out */

/*
 * The arbiter v7 frontend: annibale_haptic.c's windows and arithmetic, with the lock the
 * annibale drivers share (annibale_buttons.c owns it) in place of its private one.
 */
#define ARB_CORE_PHYS           0x0c400000UL
#define ARB_CORE_SIZE           0x003000UL
#define ARB_CNFG_PHYS           0x0c42d000UL
#define ARB_CNFG_SIZE           0x004000UL
#define ARB_CHNLS_PHYS          0x0c500000UL
#define ARB_CHNLS_SIZE          0x00010000UL
#define ARB_OBSRV_PHYS          0x0c440000UL
#define ARB_OBSRV_SIZE          0x0080000UL

#define ARB_VERSION             0x000
#define ARB_VERSION_7           0x70000000u
#define ARB_FEATURES            0x004           /* [10:0] the number of channels */
#define ARB_SYSTEM              0
#define APID_MAX                ((ARB_CORE_SIZE - 0x2000) / 4)
#define APID_MAP(n)             (0x2000UL + 4 * (n))    /* core: [19:8] the peripheral (slave and function) */
#define APID_OWNER(n)           (4 * (n))               /* cnfg: [2:0] the system that may write through it */

#define CH_CMD                  0x00
#define CH_STATUS               0x08
#define CH_WDATA0               0x10
#define CH_RDATA0               0x18
#define CH_RDATA1               0x1c
#define STATUS_DONE             0x1
#define STATUS_FAILURE          0x2
#define STATUS_DENIED           0x4
#define STATUS_DROPPED          0x8
#define CMD_OPC(opc)            ((uint32_t)(opc) << 27)
#define CMD_REG(reg)            (((uint32_t)(reg) & 0xff) << 4)
#define OPC_EXT_WRITEL          0
#define OPC_EXT_READL           1
#define OPC_EXT_WRITE           2
#define OPC_EXT_READ            13
#define TRANSACT_TIMEOUT_US     2000

static vm_offset_t arb_core, arb_cnfg, arb_chnls, arb_obsrvr;

/* the one peripheral: slave 1 function 0xee, the device tree's reg <0xee00 ...> */
#define FLASH_PPID              0x1ee
#define FLASH_FUNCTION          0xee

static struct {
	uint16_t        apid;
	bool            found, exact, ours;       /* the entry, the device tree's ppid byte for byte, ours to write */
	unsigned        owner;                    /* the system that may write through it */
} flash;

static int say(const char *format, ...) __printflike(1, 2);

/* every line to the loader's log as well (and to the console when that may be written) */
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

/*
 * One transfer at a time through the arbiter, as the buttons' file describes it: reads through
 * the read window of the module's channel, writes through the write window of the same. The
 * lock is taken inside, so the callers do not have to think about the other readers.
 */
static const char *
spmi_read16(uint16_t addr, uint8_t *bytes, uint32_t count)
{
	vm_offset_t channel;
	uint64_t start = mach_absolute_time(), now, interval;
	uint32_t status = 0, data[2] = { 0, 0 };
	const char *problem = NULL;

	if (!flash.found) {
		return "the module has no channel yet";
	}
	nanoseconds_to_absolutetime(TRANSACT_TIMEOUT_US * (uint64_t)NSEC_PER_USEC, &interval);
	channel = arb_obsrvr + 0x8000UL * ARB_SYSTEM + 0x20UL * flash.apid;
	annibale_spmi_lock();
	wr(channel + CH_CMD, CMD_OPC(count > 4 ? OPC_EXT_READL : OPC_EXT_READ) | CMD_REG(addr) | (count - 1));
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
	return problem;
}

static const char *
spmi_write16(uint16_t addr, uint8_t value)
{
	vm_offset_t channel;
	uint64_t start = mach_absolute_time(), now, interval;
	uint32_t status = 0;
	const char *problem = NULL;

	if (!flash.found) {
		return "the module has no channel yet";
	}
	if (!flash.ours) {
		return "the arbiter lets another system write the module";
	}
	nanoseconds_to_absolutetime(TRANSACT_TIMEOUT_US * (uint64_t)NSEC_PER_USEC, &interval);
	channel = arb_chnls + 0x1000UL * flash.apid;
	annibale_spmi_lock();
	wr(channel + CH_WDATA0, value);
	wr(channel + CH_CMD, CMD_OPC(OPC_EXT_WRITE) | CMD_REG(addr) | (1 - 1));
	do {
		status = rd(channel + CH_STATUS);
		now = mach_absolute_time();
	} while (!(status & STATUS_DONE) && now - start < interval);
	annibale_spmi_unlock();

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

/* one register with the house rule: the write is read back and compared; a surprise breaks the run */
static bool
flash_get(uint16_t reg, uint8_t *bytes, uint32_t count)
{
	const char *problem = spmi_read16(reg, bytes, count);

	if (problem != NULL) {
		say("torch: reading %#06x gave: %s\n", reg, problem);
		broken = true;
		return false;
	}
	return true;
}

static bool
flash_put(uint16_t reg, uint8_t value)
{
	uint8_t back = 0;

	if (spmi_write16(reg, value) != NULL || !flash_get(reg, &back, 1)) {
		say("torch: writing %#04x = %#04x did not go through\n", reg, value);
		broken = true;
		return false;
	}
	if (back != value) {
		say("torch: %#04x = %#04x read back %#04x\n", reg, value, back);
		broken = true;
		return false;
	}
	say("torch: %#04x = %#04x\n", reg, value);
	return true;
}

/* ---- the module's registers (leds-qti-flash.c, relative to the function's base) -------------------- */

#define FLASH_TYPE              0x04            /* 0x18: a flash LED module (redwood_torch.c 277) */
#define FLASH_TYPE_VALUE        0x18
#define FLASH_SUBTYPE           0x05            /* 0x07: four channels (FLASH_LED_4_CHAN) */
#define FLASH_SUBTYPE_VALUE     0x07
#define FLASH_STATUS1           0x06            /* per channel: the LED open, the LED shorted */
#define FLASH_STATUS2           0x07            /* the supply drooped (VPH) */
#define FLASH_INT_RT_STS        0x10            /* [0] a fault, as it is now */
#define FLASH_INT_LATCHED       0x18
#define FLASH_INT_PENDING       0x19
#define FLASH_SAFETY_TIMER      0x3e            /* channel id added; [7] the watchdog armed, [6:0] ms/10 - 1 */
#define FLASH_ITARGET           0x42            /* channel id added; the current, steps of IRESOLUTION */
#define FLASH_ENABLE_CONTROL    0x46            /* [7] the module's enable (FLASH_MODULE_ENABLE) */
#define FLASH_IRESOLUTION       0x49            /* per channel: 0 for 12.5 mA, 1 for 5 mA */
#define FLASH_STROBE_CTRL       0x4a            /* channel id added; [6:4] and [2]: whose strobe line */
#define FLASH_EN_LED_CTRL       0x4e            /* a bit per channel, lit */

#define IRES_MA                 12500           /* 12.5 mA as the device tree's qcom,ires-ua 0x30d4 */
#define TORCH_MAX_MA            500             /* the device tree's torch0 max-current-ma 0x1f4 */

/*
 * Bringing the module up: the arbiter's windows, its table, the module's channel, and the
 * module's answer. Made once, by the test thread or by the first call of a primitive; from then
 * on `configured` says the module was found and known, `broken` that something said surprise
 * since. Only channel 0 is ever touched -- the rear LED, the one the device tree's torch0 names.
 */
static LCK_GRP_DECLARE(torch_hv_lck_grp, "redwood-torch-hv");
static LCK_MTX_DECLARE(torch_hv_lck, &torch_hv_lck_grp);

static bool
bring_up_locked(void)
{
	uint32_t version, channels;
	uint8_t b[8];

	if (configured || broken) {
		return configured;
	}
	say("torch: mapping the PMIC arbiter (%#lx, %#lx, %#lx, %#lx)\n", ARB_CORE_PHYS, ARB_CNFG_PHYS, ARB_CHNLS_PHYS,
	    ARB_OBSRV_PHYS);
	arb_core = ml_io_map(ARB_CORE_PHYS, ARB_CORE_SIZE);
	arb_cnfg = ml_io_map(ARB_CNFG_PHYS, ARB_CNFG_SIZE);
	arb_chnls = ml_io_map(ARB_CHNLS_PHYS, ARB_CHNLS_SIZE);
	arb_obsrvr = ml_io_map(ARB_OBSRV_PHYS, ARB_OBSRV_SIZE);
	if (arb_core == 0 || arb_cnfg == 0 || arb_chnls == 0 || arb_obsrvr == 0) {
		say("torch: the arbiter's windows cannot be mapped: no flashlight\n");
		broken = true;
		return false;
	}
	version = rd(arb_core + ARB_VERSION);
	channels = rd(arb_core + ARB_FEATURES) & 0x7ff;
	say("torch: the arbiter is version %#010x with %u channels\n", version, channels);
	if (version < ARB_VERSION_7) {
		say("torch: this layout (v7) was not expected: nothing more is done\n");
		broken = true;
		return false;
	}
	if (channels > APID_MAX) {
		channels = APID_MAX;
	}
	for (uint32_t n = 0; n < channels; n++) {
		uint32_t map = rd(arb_core + APID_MAP(n)), ppid;
		bool exact;
		unsigned owner;

		if (map == 0 || (((map >> 8) & 0xfff) & 0xff) != FLASH_FUNCTION) {
			continue;
		}
		ppid = (map >> 8) & 0xfff;
		exact = ppid == FLASH_PPID;
		owner = rd(arb_cnfg + APID_OWNER(n)) & 7;
		say("torch: channel %u is flash module %#x%s, written by system %u%s\n", n, ppid,
		    exact ? "" : " (not the ppid the device tree gives)", owner, (map & (1u << 24)) ? ", which also gets its interrupts" : "");
		/* take the exact one; failing that prefer one ours to write; failing that the first */
		if (!flash.found || (exact && !flash.exact) ||
		    (exact == flash.exact && owner == ARB_SYSTEM && flash.owner != ARB_SYSTEM)) {
			flash.found = true;
			flash.exact = exact;
			flash.owner = owner;
			flash.apid = (uint16_t)n;
			flash.ours = owner == ARB_SYSTEM;
		}
	}
	if (!flash.found) {
		say("torch: no channel of the arbiter is the flash module (slave 1, function %#x)\n", FLASH_FUNCTION);
		broken = true;
		return false;
	}
	if (!flash_get(FLASH_TYPE, b, 2)) {
		return false;
	}
	say("torch: the module says type %#02x, subtype %#02x\n", b[0], b[1]);
	if (b[0] != FLASH_TYPE_VALUE || b[1] != FLASH_SUBTYPE_VALUE) {
		say("torch: that is not the module this driver knows (type %#02x, subtype %#02x): nothing is written\n",
		    FLASH_TYPE_VALUE, FLASH_SUBTYPE_VALUE);
		broken = true;
		return false;
	}
	configured = true;
	return true;
}

/* everything the module says, said: what the registers are as found, and as left */
static void
show_state(const char *when)
{
	uint8_t id[2], st[8], ints[8], lat[2], timers[8], ctl[8], en = 0;

	if (!flash_get(FLASH_TYPE, id, 2) || !flash_get(FLASH_STATUS1, st, 2) || !flash_get(FLASH_INT_RT_STS, ints, 8) ||
	    !flash_get(FLASH_INT_LATCHED, lat, 2) || !flash_get(FLASH_SAFETY_TIMER, timers, 8) ||
	    !flash_get(FLASH_ENABLE_CONTROL, ctl, 8) || !flash_get(FLASH_EN_LED_CTRL, &en, 1)) {
		return;
	}
	say("torch, %s: type %#02x subtype %#02x status1 %#02x status2 %#02x | rt_sts %#02x latched %#02x pending %#02x | "
	    "safety0 %#02x itarget0 %#02x | enable %#02x ires %#02x strobe0 %#02x en_led %#02x\n", when, id[0], id[1], st[0],
	    st[1], ints[0], lat[0], lat[1], timers[0], timers[4], ctl[0], ctl[3], ctl[4], en);
}

/*
 * Whether the module may be lit now: dark as it should be, its strobe the software's as
 * Android leaves it, no fault in real time. What the registers hold is said either way; a
 * latched fault from before (an old history) does not stop the light, a real-time one while
 * the module is dark does -- clearing latched status has no documented write here and this
 * driver does not guess at it (TODO, first look at a real phone).
 */
static bool
may_light(void)
{
	uint8_t rt = 0, latched = 0, pending = 0, enable = 0, strobe = 0, status[2];

	if (!flash_get(FLASH_INT_RT_STS, &rt, 1) || !flash_get(FLASH_INT_LATCHED, &latched, 1) ||
	    !flash_get(FLASH_INT_PENDING, &pending, 1) || !flash_get(FLASH_ENABLE_CONTROL, &enable, 1) ||
	    !flash_get(FLASH_STROBE_CTRL, &strobe, 1) || !flash_get(FLASH_STATUS1, status, 2)) {
		return false;
	}
	say("torch: before lighting: rt_sts %#02x, latched %#02x, pending %#02x, status1 %#02x, status2 %#02x, enable %#02x, strobe %#02x\n",
	    rt, latched, pending, status[0], status[1], enable, strobe);
	if (enable & 0x80) {
		say("torch: the module is already enabled, by the firmware or by someone else: not fighting over it\n");
		return false;
	}
	if ((strobe & 0x74) != 0) {
		say("torch: channel 0's strobe is not the software's (strobe ctrl %#02x); this driver only knows the "
		    "software's: nothing is lit\n", strobe);
		return false;
	}
	if (rt & 0x01) {
		say("torch: the module reports a fault in real time while dark: not lighting. The latched fault's clear "
		    "register is not documented; this driver does not guess at it\n");
		return false;
	}
	if (latched || pending) {
		say("torch: an old fault is latched (%#02x) or pending (%#02x) with nothing in real time: history, lighting anyway\n",
		    latched, pending);
	}
	return true;
}

/*
 * The light, and its opposite, both with every write read back. The caller holds
 * torch_hv_lck. A safety timer the firmware left armed is cleared for the time being -- the
 * vendor driver's own disable path clears that bit, and a torch is guarded in software on this
 * family (leds-qti-flash.c arms the timer for flashes only) -- and restored on the way out.
 */
static bool
light_locked(uint32_t ma)
{
	uint8_t b[2];
	unsigned code;

	if (broken) {
		return false;
	}
	if (lit) {
		say("torch: something is lit already: not lighting twice\n");
		return false;
	}
	if (ma == 0) {
		ma = 1;
	}
	if (ma > TORCH_MAX_MA) {
		ma = TORCH_MAX_MA;
	}
	if (!may_light()) {
		return false;
	}
	/* the watchdog, for as long as it is given a chance to be right: off, as a torch's is */
	if (!flash_get(FLASH_SAFETY_TIMER, b, 1)) {
		return false;
	}
	if (b[0] & 0x80) {
		timer_original = b[0];
		if (!flash_put(FLASH_SAFETY_TIMER, (uint8_t)(b[0] & ~0x80u))) {
			return false;
		}
		timer_cleared = true;
	}
	/* the current step of the device tree (12.5 mA): clear if firmware left it at 5 mA */
	if (!flash_get(FLASH_IRESOLUTION, b, 1)) {
		return false;
	}
	if (b[0] & 0x01) {
		say("torch: channel 0's resolution was left at 5 mA: asking for the device tree's 12.5 mA\n");
		if (!flash_put(FLASH_IRESOLUTION, (uint8_t)(b[0] & ~0x01u))) {
			return false;
		}
	}
	code = (unsigned)((ma * 1000u + IRES_MA / 2) / IRES_MA);
	if (code == 0) {
		code = 1;                           /* the smallest current there is */
	}
	code--;                                   /* ITARGET code: round(mA / 12.5) - 1, redwood_torch.c */
	if (!flash_put(FLASH_ITARGET, (uint8_t)code) || !flash_put(FLASH_ENABLE_CONTROL, 0x80)) {
		(void)spmi_write16(FLASH_ITARGET, 0);
		return false;
	}
	if (!flash_get(FLASH_EN_LED_CTRL, b, 1) || !flash_put(FLASH_EN_LED_CTRL, (uint8_t)(b[0] | 0x01))) {
		(void)spmi_write16(FLASH_ENABLE_CONTROL, 0);
		(void)spmi_write16(FLASH_ITARGET, 0);
		return false;
	}
	lit = true;
	say("torch: channel 0 is lit at %u mA (itarget %#02x)\n", ma, code);
	return true;
}

static void
dark_locked(void)
{
	uint8_t b[8];

	if (!lit) {
		return;
	}
	if (flash_get(FLASH_EN_LED_CTRL, b, 1)) {
		(void)flash_put(FLASH_EN_LED_CTRL, (uint8_t)(b[0] & ~0x01u));
	}
	(void)spmi_write16(FLASH_ENABLE_CONTROL, 0);
	(void)spmi_write16(FLASH_ITARGET, 0);
	if (timer_cleared) {
		(void)spmi_write16(FLASH_SAFETY_TIMER, timer_original);
		timer_cleared = false;
	}
	lit = false;
	if (flash_get(FLASH_ENABLE_CONTROL, b, 8)) {
		say("torch: the module is dark again: enable %#02x, ires %#02x, strobe %#02x, itarget %#02x %#02x %#02x %#02x\n",
		    b[0], b[1], b[2], b[3], b[4], b[5], b[6]);
	}
}

/* ---- what the boot-arg asks ---------------------------------------------------------------------------- */

static void
torch_thread(__unused void *parameter, __unused wait_result_t result)
{
	uint64_t until;
	uint64_t interval;
	unsigned ticks = 0;

	lck_mtx_lock(&torch_hv_lck);
	if (!bring_up_locked()) {
		lck_mtx_unlock(&torch_hv_lck);
		goto done;
	}
	show_state("as found");
	if (!light_locked(milliamps)) {
		lck_mtx_unlock(&torch_hv_lck);
		goto done;
	}
	say("torch: lighting for %u s; the module's own flags watched 10 times a second\n", secs);
	lck_mtx_unlock(&torch_hv_lck);

	nanoseconds_to_absolutetime((uint64_t)secs * NSEC_PER_SEC, &interval);
	until = mach_absolute_time() + interval;
	while (mach_absolute_time() < until) {
		uint8_t rt = 0, st[2];
		int temperature;

		wait_ms(100);
		ticks++;
		lck_mtx_lock(&torch_hv_lck);
		if (!lit) {
			say("torch: the light went out on its own: someone else owns the module\n");
			broken = true;
			lck_mtx_unlock(&torch_hv_lck);
			goto done;
		}
		if (spmi_read16(FLASH_INT_RT_STS, &rt, 1) != NULL) {
			broken = true;
			dark_locked();
			lck_mtx_unlock(&torch_hv_lck);
			goto done;
		}
		if (rt & 0x01) {
			(void)spmi_read16(FLASH_STATUS1, st, 2);
			say("torch: the module raised a fault while lit (status1 %#02x, status2 %#02x): the light goes off "
			    "and stays off\n", st[0], st[1]);
			dark_locked();
			broken = true;
			lck_mtx_unlock(&torch_hv_lck);
			goto done;
		}
		/* the battery's 45 C guard, as redwood_torch.c's; -273 while nothing knows */
		if ((temperature = redwood_battery_temperature()) > 45) {
			say("torch: the battery reads %d C, over Android's 45 C for the torch: the light goes off\n", temperature);
			dark_locked();
			broken = true;
			lck_mtx_unlock(&torch_hv_lck);
			goto done;
		}
		if (ticks % 10 == 0) {
			say("torch: still lit, %u of %u s over\n", ticks / 10, secs);
		}
		lck_mtx_unlock(&torch_hv_lck);
	}
	lck_mtx_lock(&torch_hv_lck);
	dark_locked();
	lck_mtx_unlock(&torch_hv_lck);
	say("torch: the blind test is over: if the camera flash lit for %u s and went dark by itself, this worked\n", secs);

done:
	thread_terminate(current_thread());
}

void
annibale_torch_init(void)
{
	thread_t thread;

	if (!PE_parse_boot_argn("torch_secs", &secs, sizeof(secs)) || secs == 0) {
		return;
	}
	if (secs > 5) {
		secs = 5;
	}
	PE_parse_boot_argn("torch_ma", &milliamps, sizeof(milliamps));
	if (milliamps == 0 || milliamps > TORCH_MAX_MA) {
		milliamps = 125;                  /* Android's torch current, as redwood's */
	}
	if (kernel_thread_start(torch_thread, NULL, &thread) == KERN_SUCCESS) {
		thread_deallocate(thread);
	}
}

/* ---- what the camera will ask ---------------------------------------------------------------------------- */

bool
annibale_torch_low_on(uint32_t milliamps_wanted)
{
	bool done;

	lck_mtx_lock(&torch_hv_lck);
	done = bring_up_locked() && light_locked(milliamps_wanted);
	lck_mtx_unlock(&torch_hv_lck);
	return done;
}

void
annibale_torch_low_off(void)
{
	lck_mtx_lock(&torch_hv_lck);
	dark_locked();
	lck_mtx_unlock(&torch_hv_lck);
}

/*
 * At the run's end and on a panic's way out: the light off and, if it had cleared one, the
 * safety timer given back as it was. No readbacks here -- on that way out the log may be
 * closing, and a stuck light is stopped by the writes themselves (redwood_torch.c quiesce does
 * the same). The channel states are said one last time for the log's end.
 */
void
annibale_torch_quiesce(void)
{
	bool was = lit;

	if (!configured) {
		return;
	}
	leaving = true;
	if (was) {
		(void)spmi_write16(FLASH_EN_LED_CTRL, 0x00);
		(void)spmi_write16(FLASH_ENABLE_CONTROL, 0x00);
		(void)spmi_write16(FLASH_ITARGET, 0x00);
		lit = false;
	}
	if (timer_cleared) {
		(void)spmi_write16(FLASH_SAFETY_TIMER, timer_original);
		timer_cleared = false;
	}
	if (was) {
		say("torch: at the run's end: the light that was lit is put out\n");
	}
}
