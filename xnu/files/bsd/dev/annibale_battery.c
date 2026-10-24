/*
 * annibale: TI BQ27Z561 at 0x55 on QUP1 SE5, SDA=GPIO52, SCL=GPIO53.
 * Read standard commands with open-drain GPIO and restore both pins after each
 * sample. Gauge and charger configuration remain firmware-owned.
 * Sources: live Android DT/driver and TI SLUUBO7 chapters 10 and 13.
 */
#include <kern/clock.h>
#include <kern/thread.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <pexpert/pexpert.h>
#include <pexpert/device_tree.h>
#include <sys/redwood_audio.h>
#include <sys/redwood_input.h>
#include <sys/systm.h>

extern vm_offset_t ml_io_map(vm_offset_t, vm_size_t);
extern kern_return_t thread_terminate(thread_t);
#define SDA 0
#define SCL 1
#define OUTPUT (1u << 9)
#define GAUGE_ADDRESS 0x55
static vm_offset_t pins;
static uint32_t saved_cfg[2], saved_io[2], input_cfg[2];
static bool bus_ok;

static uint32_t
rd(unsigned pin, unsigned offset)
{
	return *(volatile uint32_t *)(pins + pin * 0x1000 + offset);
}

static void
wr(unsigned pin, unsigned offset, uint32_t value)
{
	*(volatile uint32_t *)(pins + pin * 0x1000 + offset) = value;
	__asm__ volatile("dsb sy" ::: "memory");
}

static void
pause_bus(void)
{
	delay_for_interval(5, NSEC_PER_USEC);
}

/* A high bit releases the line; the driver only drives low. */
static void
line(unsigned pin, bool high)
{
	wr(pin, 0, input_cfg[pin] | (high ? 0 : OUTPUT));
	pause_bus();
}

static bool
clock_high(void)
{
	line(SCL, true);
	/* The gauge may stretch SCL for 40.08 ms during its own flash update. */
	for (unsigned i = 0; i < 10000; i++) {
		if (rd(SCL, 4) & 1) { return true; }
		pause_bus();
	}
	bus_ok = false;
	return false;
}

static bool
start(void)
{
	line(SDA, true);
	if (!clock_high() || !(rd(SDA, 4) & 1)) { bus_ok = false; return false; }
	line(SDA, false);
	line(SCL, false);
	return true;
}

static void
stop(void)
{
	line(SCL, false);
	line(SDA, false);
	clock_high();
	line(SDA, true);
}

static bool
send_byte(uint8_t value)
{
	for (unsigned bit = 0; bit < 8; bit++) {
		line(SDA, (value & 0x80) != 0);
		if (!clock_high()) { return false; }
		line(SCL, false);
		value <<= 1;
	}
	line(SDA, true);
	if (!clock_high()) { return false; }
	bool acknowledged = !(rd(SDA, 4) & 1);
	line(SCL, false);
	return acknowledged;
}

static uint8_t
read_byte(bool acknowledge)
{
	uint8_t value = 0;
	line(SDA, true);
	for (unsigned bit = 0; bit < 8; bit++) {
		if (!clock_high()) { return 0; }
		value = (value << 1) | (rd(SDA, 4) & 1);
		line(SCL, false);
	}
	line(SDA, !acknowledge);
	clock_high();
	line(SCL, false);
	line(SDA, true);
	return value;
}

static bool
read_word(uint8_t reg, uint16_t *value)
{
	uint8_t low = 0, high = 0;
	bus_ok = true;
	bool acknowledged = start() && send_byte(GAUGE_ADDRESS << 1) && send_byte(reg) &&
	    start() && send_byte((GAUGE_ADDRESS << 1) | 1);
	if (acknowledged) {
		low = read_byte(true);
		high = read_byte(false);
	}
	stop();
	*value = low | ((uint16_t)high << 8);
	return acknowledged && bus_ok;
}

static bool
sample(void)
{
	static const uint8_t registers[] = {0x2c, 0x08, 0x0c, 0x06, 0x10, 0x12, 0x3c, 0x2a};
	uint16_t values[sizeof(registers)] = {0};
	unsigned count = 0;
	for (unsigned pin = 0; pin < 2; pin++) {
		saved_cfg[pin] = rd(pin, 0);
		saved_io[pin] = rd(pin, 4) & 2;
		input_cfg[pin] = (saved_cfg[pin] & ~(0x3fu | OUTPUT)) | 3;
		wr(pin, 0, input_cfg[pin]);
		wr(pin, 4, 0);
	}
	/* Only take an idle bus; the charge pump at 0x6e shares these pins. */
	if ((rd(SDA, 4) & 1) && (rd(SCL, 4) & 1)) {
		for (; count < sizeof(registers); count++) {
			if (!read_word(registers[count], &values[count])) { break; }
		}
	}
	line(SDA, true);
	line(SCL, true);
	for (unsigned pin = 0; pin < 2; pin++) {
		wr(pin, 4, saved_io[pin]);
		wr(pin, 0, saved_cfg[pin]);
	}
	if (count != sizeof(registers)) {
		printf("battery: BQ27Z561 read failed at command %#x (%u completed)\n", registers[count], count);
		return false;
	}
	if (values[0] > 100 || values[1] < 2500 || values[1] > 5000 || values[3] < 2331 || values[3] > 3582) {
		printf("battery: BQ27Z561 invalid sample: SOC=%u voltage=%u temperature=%u\n", values[0], values[1], values[3]);
		return false;
	}
	struct redwood_battery_state state = {0};
	state.size = sizeof(state);
	state.percent_hundredths = values[0] * 100;
	state.voltage_uv = values[1] * 1000;
	state.current_ua = (int16_t)values[2] * 1000;
	state.temperature_hundredths = values[3] * 10 - 27315;
	state.present = 1;
	/* Current confirms energy flow; USB connection and limits need a separate source. */
	state.status = state.current_ua > 0 ? 1 : state.current_ua < 0 ? 2 : 3;
	state.external = state.current_ua > 0;
	state.charge_uah = values[4] * 1000;
	state.full_uah = values[5] * 1000;
	state.design_uah = values[6] * 1000;
	state.cycles = values[7];
	redwood_battery_report(&state);
	int fraction = state.temperature_hundredths % 100;
	printf("battery: BQ27Z561 SOC=%u%% V=%u mV I=%d mA T=%d.%02d C\n", values[0], values[1],
	    (int16_t)values[2], state.temperature_hundredths / 100, fraction < 0 ? -fraction : fraction);
	return true;
}

static void
battery_thread(__unused void *parameter, __unused wait_result_t result)
{
	printf("battery: BQ27Z561 on GPIO52/53, address %#x; reading standard commands\n", GAUGE_ADDRESS);
	pins = ml_io_map(0x0f134000UL, 0x2000);
	if (pins) {
		for (unsigned failures = 0; failures < 3;) {
			failures = sample() ? 0 : failures + 1;
			delay_for_interval(5000, NSEC_PER_MSEC);
		}
	}
	printf("battery: BQ27Z561 polling stopped\n");
	thread_terminate(current_thread());
}

void
annibale_battery_init(void)
{
	uint32_t enabled = 0;
	thread_t thread;
	DTEntry entry;
	const void *property;
	unsigned size;
	if (!PE_parse_boot_argn("battery", &enabled, sizeof(enabled)) || !enabled) { return; }
	if (pe_arm_get_soc_base_phys() != 0x400000 ||
	    SecureDTLookupEntry(NULL, "/arm-io/gic", &entry) != kSuccess ||
	    SecureDTGetProperty(entry, "reg", &property, &size) != kSuccess || size != 4 * sizeof(uint64_t)) { return; }
	const uint64_t *reg = property;
	if (reg[0] != 0x15c00000 || reg[2] != 0x15c80000 || reg[3] != 0x200000) { return; }
	if (kernel_thread_start(battery_thread, NULL, &thread) == KERN_SUCCESS) { thread_deallocate(thread); }
}
