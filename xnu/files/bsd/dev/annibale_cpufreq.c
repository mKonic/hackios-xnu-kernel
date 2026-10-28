/*
 * The clock of the processor cores on the REDMI K90 (annibale, SM8750). The CPUCP firmware sets the
 * cores' clocks and voltages; the operating system asks it for a performance level through SCMI's
 * performance protocol (0x13), one message at a time in a channel of shared memory, with a mailbox
 * doorbell after each. Domain 0 is the six cores of cluster 0, domain 1 the two of cluster 1 (stock
 * device tree: CPU0-5 clocks = <&scmi_perf 0>, CPU6-7 <&scmi_perf 1>); on the phone both name their
 * levels by index (attribute bit 25), and the boot loader leaves domain 1 at its slowest, 1017.6 MHz.
 *
 * Boot-arg cpufreq=2 only reports: the protocol, every domain with its levels and the level it is
 * at, how fast a chain of adds runs, and every 30 s the temperatures. cpufreq=1 runs a governor for
 * each domain with a core of ours, every TICK_MS:
 *   - the busiest of its cores decides: the slowest level that would have left it 80% busy, at once
 *     when that is faster, and only after DOWN_MS of wanting less when it is slower;
 *   - a touch keeps it at BOOST_MHZ or faster for BOOST_MS;
 *   - never faster than cpufreq_mhz=N (default CEILING_MHZ) or the sustained frequency the firmware
 *     gives (its sustained level index names another level: 4320 MHz for 4089.6);
 *   - at HOT_TENTHS on any sensor of its cluster it goes to its slowest level until under
 *     COOL_TENTHS, and without a valid reading it stays there. These are far under the stock trips
 *     (95 C for cpu-1-*): the phone is not to get near those.
 * cpufreq=3 keeps the domain at the ceiling instead (still under the same temperature rule), to
 * compare with.
 *
 * Channel layout: Linux drivers/firmware/arm_scmi/shmem.c; doorbell: drivers/mailbox/qcom-cpucp-mbox.c
 * (TX_MBOX_CMD(chan) + 4; any value); addresses: the stock device tree (mmio-sram@0x17b4e000,
 * qcom,cpucp@0x17830000, channel 0 for "tx"). Temperature sensors: drivers/thermal/qcom/tsens-v2.c.
 */
#include <kern/clock.h>
#include <kern/sched_prim.h>
#include <kern/thread.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <machine/machine_routines.h>
#include <os/atomic_private.h>
#include <pexpert/pexpert.h>
#include <sys/redwood_input.h>
#include <sys/systm.h>

extern vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);
extern void loader_log_putc(char c);
extern bool ml_cpu_idle_time(int cpu_id, uint64_t *idle);

#define SCMI_SHMEM_PHYS         0x17b4e000UL
#define SCMI_SHMEM_SIZE         0x200U
#define SCMI_DOORBELL_PHYS      0x17830000UL
#define SCMI_DOORBELL_SIZE      0x300UL
#define SCMI_DOORBELL           (0x100 + 4)     /* channel 0 */
#define SHMEM_STATUS            0x04            /* bit 0: free (the platform is done with it), bit 1: error */
#define SHMEM_FLAGS             0x10            /* bit 0: an interrupt when done; we poll */
#define SHMEM_LENGTH            0x14            /* bytes of header and payload */
#define SHMEM_HEADER            0x18
#define SHMEM_PAYLOAD           0x1c
#define SHMEM_WORDS             ((SCMI_SHMEM_SIZE - SHMEM_PAYLOAD) / 4)
#define SCMI_PERF               0x13
#define PERF_VERSION            0x0
#define PERF_ATTRIBUTES         0x1
#define PERF_DOMAIN_ATTRIBUTES  0x3
#define PERF_DESCRIBE_LEVELS    0x4
#define PERF_LEVEL_SET          0x7
#define PERF_LEVEL_GET          0x8
#define PERF_LEVEL_SET_ALLOWED  (1u << 30)
#define PERF_LEVEL_INDEXING     (1u << 25)      /* (4.0) levels are asked for and told by index */
#define SCMI_BUSY               (-100)          /* the channel never came free */
#define SCMI_TIMEOUT            (-101)          /* no answer */
#define SCMI_CHANNEL_ERROR      (-102)
#define SCMI_WRONG_ANSWER       (-103)          /* the answer is to another message */
#define PERF_DOMAINS            2
#define MAX_LEVELS              32
#define MAX_CORES               8

#define TICK_MS                 50
#define DOWN_MS                 200
#define BOOST_MS                300
#define BOOST_MHZ               2500            /* (2438 on the phone: the desktop's frame times at 3072 were no better) */
#define CEILING_MHZ             3100
#define BUSY_TARGET             800             /* thousandths */
#define REPORT_MS               30000

#define TSENS_STATUS(sensor)    (0xa0 + 4 * (sensor))   /* last reading: [11:0] signed tenths of a degree */
#define TSENS_VALID             (1u << 21)
#define HOT_TENTHS              700
#define COOL_TENTHS             600
#define NO_READING              (-10000)
static const struct {
	const char *name;
	vm_offset_t phys;
	uint32_t first, last;   /* sensors (stock thermal-zones: cpu-0-*, cpuss-0-*; cpu-1-*, cpuss-1-*; gpuss-*) */
} tsens_groups[] = {
	{ "cluster 0", 0x0c228000, 1, 14 },
	{ "cluster 1", 0x0c229000, 1, 6 },
	{ "GPU", 0x0c22a000, 1, 8 },
};
#define TSENS_GROUPS            (sizeof(tsens_groups) / sizeof(tsens_groups[0]))
#define TSENS_GPU               2

static volatile uint32_t *scmi_shmem, *scmi_doorbell;
static uint32_t scmi_token, mode;
static vm_offset_t tsens_base[TSENS_GROUPS];
static uint64_t touch_at;                       /* mach_absolute_time() of the last touch; 0: none yet */
static bool governing;

/* domain N is cluster N, whose sensors are tsens_groups[N]; levels slowest first */
static struct {
	bool ours, known, refused, hot;
	uint32_t count, key[MAX_LEVELS], khz[MAX_LEVELS];       /* key: what LEVEL_SET takes (index or value) */
	uint32_t boot, ceiling, boost, now;                     /* indices into key[] and khz[] */
	uint64_t low_since;                                     /* since when a slower level would do; 0: not */
	uint64_t at[MAX_LEVELS], asking;                        /* time at each level and in LEVEL_SET, since the last report */
	uint32_t changes, hot_entries;                          /* (hot_entries: times it reached HOT_TENTHS) */
	int32_t hottest;
} perf_domain[PERF_DOMAINS];

static void
tell(const char *format, ...) __printflike(1, 2);

/* to the loader's log as well: this runs early, and the kernel's own log of a run keeps only its end */
static void
tell(const char *format, ...)
{
	char line[256];
	va_list list;

	va_start(list, format);
	vsnprintf(line, sizeof(line), format, list);
	va_end(list);
	for (const char *c = line; *c != 0; c++) {
		loader_log_putc(*c);
	}
	printf("%s", line);
}

/* Adds that each wait for the one before: about the core's clock in MHz. */
static uint32_t
chain_mhz(void)
{
	uint64_t x = 0, start, nanos;
	boolean_t enabled = ml_set_interrupts_enabled(FALSE);

	start = mach_absolute_time();
	for (uint32_t i = 0; i < 10000; i++) {
		__asm__ volatile (".rept 1000\n\tadd %0, %0, #1\n\t.endr" : "+r"(x));
	}
	absolutetime_to_nanoseconds(mach_absolute_time() - start, &nanos);
	ml_set_interrupts_enabled(enabled);
	return nanos ? (uint32_t)(10000000ULL * 1000 / nanos) : 0;
}

static bool
scmi_wait_free(uint32_t ms)
{
	uint64_t deadline;

	nanoseconds_to_absolutetime((uint64_t)ms * NSEC_PER_MSEC, &deadline);
	deadline += mach_absolute_time();
	while ((scmi_shmem[SHMEM_STATUS / 4] & 3) == 0) {
		if (mach_absolute_time() > deadline) {
			return false;
		}
		delay_for_interval(10, NSEC_PER_USEC);
	}
	return true;
}

/* One command: `in_words` out; on return *words (at most what it was) back in `out`. The SCMI status
 * (0: success), or one of SCMI_BUSY and the others above. */
static int32_t
scmi_call(uint32_t message, const uint32_t *in, uint32_t in_words, uint32_t *out, uint32_t *words)
{
	uint32_t header = message | SCMI_PERF << 10 | (scmi_token++ & 0x3ff) << 18, got;

	if (!scmi_wait_free(10)) {
		return SCMI_BUSY;
	}
	scmi_shmem[SHMEM_STATUS / 4] = 0;
	scmi_shmem[SHMEM_FLAGS / 4] = 0;
	scmi_shmem[SHMEM_LENGTH / 4] = 4 + 4 * in_words;
	scmi_shmem[SHMEM_HEADER / 4] = header;
	for (uint32_t i = 0; i < in_words; i++) {
		scmi_shmem[SHMEM_PAYLOAD / 4 + i] = in[i];
	}
	__builtin_arm_dsb(0xf);
	*scmi_doorbell = 1;
	if (!scmi_wait_free(50)) {
		return SCMI_TIMEOUT;
	}
	if (scmi_shmem[SHMEM_STATUS / 4] & 2) {
		return SCMI_CHANNEL_ERROR;
	}
	if (scmi_shmem[SHMEM_HEADER / 4] != header) {
		return SCMI_WRONG_ANSWER;
	}
	got = scmi_shmem[SHMEM_LENGTH / 4];
	got = got >= 8 ? (got - 8) / 4 : 0;
	got = MIN(got, MIN(*words, SHMEM_WORDS - 1));
	for (uint32_t i = 0; i < got; i++) {
		out[i] = scmi_shmem[SHMEM_PAYLOAD / 4 + 1 + i];
	}
	*words = got;
	return (int32_t)scmi_shmem[SHMEM_PAYLOAD / 4];
}

/* The hottest valid reading of a group, in tenths of a degree; false if none was valid. */
static bool tsens_hottest(unsigned group, int32_t *tenths);

/* For adreno8_speed_tick(): the last touch, which boosts the GPU as it boosts the processors. */
uint64_t
annibale_cpufreq_touched(void)
{
	return os_atomic_load(&touch_at, relaxed);
}

/* For adreno8_speed_tick(): the GPU group, mapped only with cpufreq=N. */
bool
annibale_gpu_temperature(int32_t *tenths)
{
	return tsens_base[TSENS_GPU] != 0 && tsens_hottest(TSENS_GPU, tenths);
}


static bool
tsens_hottest(unsigned group, int32_t *tenths)
{
	bool any = false;

	for (uint32_t s = tsens_groups[group].first; s <= tsens_groups[group].last; s++) {
		uint32_t status = *(volatile uint32_t *)(tsens_base[group] + TSENS_STATUS(s));
		int32_t t = (int32_t)(status << 20) >> 20;

		if ((status & TSENS_VALID) && (!any || t > *tenths)) {
			*tenths = t;
			any = true;
		}
	}
	return any;
}

static int
temperatures(char *line, size_t size)
{
	int at = 0;

	line[0] = 0;
	for (unsigned g = 0; g < TSENS_GROUPS && at < (int)size; g++) {
		int32_t t;

		at += tsens_hottest(g, &t) ? snprintf(line + at, size - at, "%s%s %d.%d C", g ? ", " : "", tsens_groups[g].name, t / 10,
		    (t < 0 ? -t : t) % 10) : snprintf(line + at, size - at, "%s%s no reading", g ? ", " : "", tsens_groups[g].name);
	}
	return at;
}

/* Its attributes and levels (kept, slowest first, with the ceiling and the boost floor among them) to the log. */
static void
perf_describe(uint32_t d, uint32_t version)
{
	uint32_t out[SHMEM_WORDS], words, attributes, ceiling, level = 0, cap = CEILING_MHZ;
	uint32_t entry = version >= 0x40000 ? 5 : 3;    /* words a level takes (4.0 adds a frequency and an index) */
	char name[17] = { 0 }, list[200];
	__typeof__(perf_domain[0]) *pd = &perf_domain[d];
	bool indexing;
	int32_t status;
	int at = 0;

	words = 8;
	status = scmi_call(PERF_DOMAIN_ATTRIBUTES, &d, 1, out, &words);
	if (status != 0 || words < 4) {
		tell("cpufreq: SCMI domain %u: no attributes (status %d)\n", d, status);
		return;
	}
	attributes = out[0];
	ceiling = out[2];
	PE_parse_boot_argn("cpufreq_mhz", &cap, sizeof(cap));
	if (cap != 0 && (ceiling == 0 || cap * 1000 < ceiling)) {
		ceiling = cap * 1000;
	}
	indexing = entry == 5 && (attributes & PERF_LEVEL_INDEXING);
	if (words >= 8) {
		memcpy(name, &out[4], 16);
	}
	tell("cpufreq: SCMI domain %u \"%s\": attributes %#x, rate limit %u us, sustained %u kHz at level %u\n", d, name,
	    attributes, out[1], out[2], out[3]);
	pd->count = 0;
	for (uint32_t index = 0; pd->count < MAX_LEVELS;) {
		uint32_t in[2] = { d, index }, returned, remaining;

		words = SHMEM_WORDS - 1;
		status = scmi_call(PERF_DESCRIBE_LEVELS, in, 2, out, &words);
		if (status != 0 || words < 1) {
			break;
		}
		returned = out[0] & 0xfff;
		remaining = out[0] >> 16;
		if (returned == 0 || 1 + returned * entry > words) {
			break;
		}
		for (uint32_t i = 0; i < returned && pd->count < MAX_LEVELS; i++) {
			const uint32_t *e = &out[1 + i * entry];
			uint32_t key = indexing ? e[4] : e[0], khz = entry == 5 && e[3] != 0 ? e[3] : e[0], j = pd->count++;

			for (; j > 0 && pd->khz[j - 1] > khz; j--) {
				pd->khz[j] = pd->khz[j - 1];
				pd->key[j] = pd->key[j - 1];
			}
			pd->khz[j] = khz;
			pd->key[j] = key;
		}
		index += returned;
		if (remaining == 0) {
			break;
		}
	}
	list[0] = 0;
	for (uint32_t i = 0; i < pd->count; i++) {
		if (at < (int)sizeof(list) - 16) {
			at += snprintf(list + at, sizeof(list) - at, " %u/%u", pd->khz[i] / 1000, pd->key[i]);
		}
	}
	words = 1;
	status = scmi_call(PERF_LEVEL_GET, &d, 1, &level, &words);
	pd->boot = pd->ceiling = pd->boost = 0;
	for (uint32_t i = 0; i < pd->count; i++) {
		pd->boot = pd->key[i] == level ? i : pd->boot;
		pd->ceiling = ceiling == 0 || pd->khz[i] <= ceiling ? i : pd->ceiling;
	}
	for (uint32_t i = 0; i <= pd->ceiling && i < pd->count; i++) {
		pd->boost = pd->khz[i] <= BOOST_MHZ * 1000 ? i : pd->boost;
	}
	pd->now = pd->boot;
	tell("cpufreq: SCMI domain %u: %u levels (MHz/%s):%s\n", d, pd->count, indexing ? "index" : "value", list);
	if (pd->count == 0 || status != 0 || words < 1) {
		tell("cpufreq: SCMI domain %u: level unknown (status %d)\n", d, status);
		return;
	}
	tell("cpufreq: SCMI domain %u: at %u MHz; at most %u MHz, at least %u MHz for %u ms after a touch\n", d,
	    pd->khz[pd->boot] / 1000, pd->khz[pd->ceiling] / 1000, pd->khz[pd->boost] / 1000, BOOST_MS);
	pd->known = (attributes & PERF_LEVEL_SET_ALLOWED) != 0;
}

/* The domain to level i (of its own, slowest first); its time at the old one counted up to `now`. */
static void
perf_go(uint32_t d, uint32_t i, uint64_t now, uint64_t *then)
{
	__typeof__(perf_domain[0]) *pd = &perf_domain[d];
	uint32_t in[2] = { d, pd->key[i] }, words = 0;
	uint64_t began = mach_absolute_time();
	int32_t status;

	pd->at[pd->now] += now - *then;
	*then = now;
	if (i == pd->now) {
		return;
	}
	status = scmi_call(PERF_LEVEL_SET, in, 2, NULL, &words);
	pd->asking += mach_absolute_time() - began;
	if (status != 0) {
		pd->refused = true;     /* (not asked again every tick) */
		tell("cpufreq: SCMI domain %u: level %u MHz refused (status %d); left as it is\n", d, pd->khz[i] / 1000, status);
		return;
	}
	pd->now = i;
	pd->changes++;
}

/* The level the governor wants for domain d, given its busiest core's thousandths busy. */
static uint32_t
perf_want(uint32_t d, uint32_t busy, uint64_t now, uint64_t boost_abs, bool valid, int32_t t)
{
	__typeof__(perf_domain[0]) *pd = &perf_domain[d];
	uint32_t want = 0, khz = (uint32_t)((uint64_t)pd->khz[pd->now] * busy / BUSY_TARGET);
	uint64_t touched = os_atomic_load(&touch_at, relaxed);
	bool was_hot = pd->hot;

	pd->hot = !valid || t >= HOT_TENTHS || (pd->hot && t >= COOL_TENTHS);
	pd->hot_entries += pd->hot && !was_hot;
	if (pd->hot) {
		return 0;
	}
	if (mode == 3) {
		return pd->ceiling;
	}
	while (want < pd->ceiling && pd->khz[want] < khz) {
		want++;
	}
	if (touched != 0 && now - touched < boost_abs && want < pd->boost) {
		want = pd->boost;
	}
	return want;
}

static void
report(uint64_t span)
{
	char line[256];
	int at = 0;

	for (uint32_t d = 0; d < PERF_DOMAINS; d++) {
		__typeof__(perf_domain[0]) *pd = &perf_domain[d];
		uint64_t ns;

		if (!pd->ours || !pd->known || span == 0) {
			continue;
		}
		at = snprintf(line, sizeof(line), "cpufreq: domain %u in %llu s:", d, span / NSEC_PER_SEC);
		for (uint32_t i = 0; i < pd->count && at < (int)sizeof(line) - 24; i++) {
			absolutetime_to_nanoseconds(pd->at[i], &ns);
			if (ns * 100 / span != 0) {
				at += snprintf(line + at, sizeof(line) - at, " %u MHz %llu%%", pd->khz[i] / 1000, ns * 100 / span);
			}
			pd->at[i] = 0;
		}
		absolutetime_to_nanoseconds(pd->asking, &ns);
		at += snprintf(line + at, sizeof(line) - at, "; %u changes, %llu us each; %u times at the limit; ", pd->changes,
		    pd->changes ? ns / 1000 / pd->changes : 0, pd->hot_entries);
		if (pd->hottest == NO_READING) {
			snprintf(line + at, sizeof(line) - at, "no reading\n");
		} else {
			snprintf(line + at, sizeof(line) - at, "hottest %d.%d C\n", pd->hottest / 10, (pd->hottest < 0 ? -pd->hottest : pd->hottest) % 10);
		}
		printf("%s", line);
		pd->changes = 0;
		pd->hot_entries = 0;
		pd->asking = 0;
		pd->hottest = NO_READING;
	}
	at = snprintf(line, sizeof(line), "cpufreq: ");
	temperatures(line + at, sizeof(line) - at);
	printf("%s\n", line);
}

static void
governor_thread(void *arg __unused, wait_result_t wr __unused)
{
	const ml_topology_info_t *topology = ml_get_topology_info();
	uint64_t idle_before[MAX_CORES] = { 0 }, then = mach_absolute_time(), level_since[PERF_DOMAINS], boost_abs, down_abs;
	uint64_t report_ns = 0;

	nanoseconds_to_absolutetime(BOOST_MS * NSEC_PER_MSEC, &boost_abs);
	nanoseconds_to_absolutetime(DOWN_MS * NSEC_PER_MSEC, &down_abs);
	for (uint32_t d = 0; d < PERF_DOMAINS; d++) {
		level_since[d] = then;
		perf_domain[d].hottest = NO_READING;
	}
	for (unsigned int i = 0; i < topology->num_cpus && i < MAX_CORES; i++) {
		ml_cpu_idle_time((int)i, &idle_before[i]);
	}
	for (;;) {
		uint32_t busy[PERF_DOMAINS] = { 0 };
		uint64_t now, span, ns;

		assert_wait_timeout((event_t)&touch_at, THREAD_UNINT, TICK_MS, NSEC_PER_MSEC);
		thread_block(THREAD_CONTINUE_NULL);
		now = mach_absolute_time();
		span = now - then;
		if (span == 0) {
			continue;
		}
		for (unsigned int i = 0; i < topology->num_cpus && i < MAX_CORES; i++) {
			uint32_t d = (topology->cpus[i].phys_id >> 16) & 0xff;
			uint64_t idle = 0, idle_span;

			if (d >= PERF_DOMAINS || !ml_cpu_idle_time((int)i, &idle)) {
				continue;
			}
			idle_span = MIN(idle - idle_before[i], span);
			idle_before[i] = idle;
			busy[d] = MAX(busy[d], (uint32_t)((span - idle_span) * 1000 / span));
		}
		for (uint32_t d = 0; d < PERF_DOMAINS; d++) {
			__typeof__(perf_domain[0]) *pd = &perf_domain[d];
			int32_t t = 0;
			bool valid = tsens_hottest(d, &t);
			uint32_t want;

			if (!pd->ours || !pd->known) {
				continue;
			}
			pd->hottest = valid ? MAX(pd->hottest, t) : pd->hottest;
			if (!governing || pd->refused) {
				pd->at[pd->now] += now - level_since[d];
				level_since[d] = now;
				continue;
			}
			want = perf_want(d, busy[d], now, boost_abs, valid, t);
			if (want > pd->now || pd->hot) {
				pd->low_since = 0;
				perf_go(d, want, now, &level_since[d]);
			} else if (want < pd->now) {
				pd->low_since = pd->low_since ? pd->low_since : now;
				perf_go(d, now - pd->low_since >= down_abs ? want : pd->now, now, &level_since[d]);
				pd->low_since = pd->now == want ? 0 : pd->low_since;
			} else {
				pd->low_since = 0;
				perf_go(d, want, now, &level_since[d]);
			}
		}
		absolutetime_to_nanoseconds(span, &ns);
		report_ns += ns;
		then = now;
		if (report_ns >= REPORT_MS * NSEC_PER_MSEC) {
			report(report_ns);
			report_ns = 0;
		}
	}
}

/* A touch (redwood_input.c's touch_post()): the governor raises the level now rather than at its next tick. */
void
annibale_cpufreq_touch(void)
{
	uint64_t now = mach_absolute_time(), last = os_atomic_xchg(&touch_at, now, relaxed), boost_abs;

	nanoseconds_to_absolutetime(BOOST_MS * NSEC_PER_MSEC, &boost_abs);
	if (governing && (last == 0 || now - last >= boost_abs)) {
		thread_wakeup((event_t)&touch_at);      /* (a touch in a boost already given needs nothing more) */
	}
}

void
annibale_cpufreq_init(void)
{
	const ml_topology_info_t *topology = ml_get_topology_info();
	uint32_t out[4], words, version, domains;
	char line[160];
	thread_t thread;
	int32_t status;

	if (!PE_parse_boot_argn("cpufreq", &mode, sizeof(mode)) || mode < 1 || mode > 3) {
		return;
	}
	for (unsigned g = 0; g < TSENS_GROUPS; g++) {
		tsens_base[g] = ml_io_map(tsens_groups[g].phys, 0x200);
	}
	for (unsigned int i = 0; i < topology->num_cpus; i++) {
		uint32_t cluster = (topology->cpus[i].phys_id >> 16) & 0xff;

		if (cluster < PERF_DOMAINS) {
			perf_domain[cluster].ours = true;
		}
	}
	scmi_shmem = (volatile uint32_t *)ml_io_map(SCMI_SHMEM_PHYS, SCMI_SHMEM_SIZE);
	scmi_doorbell = (volatile uint32_t *)(ml_io_map(SCMI_DOORBELL_PHYS, SCMI_DOORBELL_SIZE) + SCMI_DOORBELL);
	temperatures(line, sizeof(line));
	tell("cpufreq: %s; SCMI channel status %#x; a chain of adds at %u MHz on core %#llx\n", line, scmi_shmem[SHMEM_STATUS / 4],
	    chain_mhz(), __builtin_arm_rsr64("MPIDR_EL1") & 0xffffff);
	words = 1;
	status = scmi_call(PERF_VERSION, NULL, 0, out, &words);
	if (status != 0 || words < 1) {
		tell("cpufreq: SCMI performance protocol: no answer (status %d)\n", status);
		return;
	}
	version = out[0];
	words = 4;
	status = scmi_call(PERF_ATTRIBUTES, NULL, 0, out, &words);
	domains = status == 0 && words >= 1 ? out[0] & 0xffff : 0;
	tell("cpufreq: SCMI performance protocol %u.%u, %u domains (status %d)\n", version >> 16, version & 0xffff, domains, status);
	for (uint32_t d = 0; d < domains && d < PERF_DOMAINS; d++) {
		perf_describe(d, version);
	}
	governing = mode == 1 || mode == 3;
	if (kernel_thread_start(governor_thread, NULL, &thread) == KERN_SUCCESS) {
		thread_deallocate(thread);
	}
}
