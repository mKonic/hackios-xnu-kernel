/* SM8750 GPU power, clocks, CMD DB and SCM initialization. */
#include "adreno8_private.h"

#define printf adreno8_say

/* ---- step 1: what feeds the GPU from outside --------------------------------------------------------- */

/*
 * dt: clock-controller@100000. Of the GPU's clocks here only gcc_ddrss_gpu_axi (0x71150, the
 * memory bus tap) is interesting and writable: gcc_gpu_cfg_ahb, the clock of the GPU clock
 * controller's own registers, is on this SoC among the clock controller's PROTECTED clocks --
 * the hypervisor's, with no clock driver entry in Linux at all -- so it is neither written nor
 * depended on; the loader runs it, and step 2 reads whether that is still true.
 */

static struct adreno8_block adreno8_gcc = { "gcc", 0x00100000, 0x80000 };

/* dt: reserved-memory cmd_db@81c60000, 0x20000: the boot loader's list of what RPMh can switch */

static const struct adreno8_cmd_db_header *adreno8_cmd_db;

/*
 * The entry of the boot loader's list that names a resource: where RPMh has it (0: nowhere) and
 * its raw data. For a supply rail the data is the list of levels it knows, two bytes each; for a
 * bus monitor one struct of eight bytes; the GMU's firmware is told the levels by their position
 * in the list. (redwood_adreno.c's rpmh_resource(), widened to return the raw data too.)
 */

uint32_t
adreno8_rpmh_lookup(const char *name, uint16_t *levels, unsigned *level_count, const uint8_t **data, unsigned *data_length)
{
	const struct adreno8_cmd_db_header *db = adreno8_cmd_db;
	char id[8] = { 0 };

	if (level_count != NULL) {
		*level_count = 0;
	}
	if (data_length != NULL) {
		*data_length = 0;
	}
	if (db == NULL) {
		db = adreno8_cmd_db = (const struct adreno8_cmd_db_header *)ml_io_map_wcomb(CMD_DB_PHYS, CMD_DB_SIZE);  /* memory, not registers */
	}
	if (db->magic[0] != 0xdb || db->magic[1] != 0x30 || db->magic[2] != 0x03 || db->magic[3] != 0x0c) {
		printf("adreno8: %u: no cmd_db at %#lx (magic %02x %02x %02x %02x)\n", adreno8_step, CMD_DB_PHYS, db->magic[0],
		    db->magic[1], db->magic[2], db->magic[3]);
		return 0;
	}
	strlcpy(id, name, sizeof(id));
	for (unsigned s = 0; s < 8; s++) {
		const struct adreno8_cmd_db_slave *slave = &db->slave[s];
		const struct adreno8_cmd_db_entry *entry = (const struct adreno8_cmd_db_entry *)(db->data + slave->header_offset);

		if (slave->id == 0 || slave->count > 512) {
			continue;
		}
		for (unsigned i = 0; i < slave->count; i++) {
			const uint8_t *at = db->data + slave->data_offset + entry[i].offset;
			unsigned count = entry[i].length / 2u;

			if (memcmp(entry[i].id, id, sizeof(id)) != 0) {
				continue;
			}
			if ((unsigned)slave->data_offset + entry[i].offset + entry[i].length > CMD_DB_SIZE - 0x1000) {
				count = 0;
			}
			for (unsigned l = 0; levels != NULL && l < count && l < RPMH_LEVELS; l++) {
				levels[l] = (uint16_t)(at[2 * l] | at[2 * l + 1] << 8);
				if (l > 0 && levels[l] == 0) {
					break;
				}
				*level_count = l + 1;
			}
			if (data != NULL) {
				*data = at;
			}
			if (data_length != NULL) {
				*data_length = entry[i].length;
			}
			return entry[i].address;
		}
	}
	return 0;
}

/* a bus monitor's description, one per resource (cmd-db.c: struct bcm_db) */

void
adreno8_show_rpmh_resource(const char *name)
{
	uint16_t level[RPMH_LEVELS];
	unsigned count = 0;
	uint32_t address = adreno8_rpmh_lookup(name, level, &count, NULL, NULL);
	char levels[160];
	int at = 0;

	if (address == 0) {
		printf("adreno8: %u: RPMh has no \"%s\"\n", adreno8_step, name);
		return;
	}
	levels[0] = 0;
	for (unsigned l = 0; l < count && at < (int)sizeof(levels) - 8; l++) {
		at += snprintf(levels + at, sizeof(levels) - (size_t)at, " %u", level[l]);
	}
	printf("adreno8: %u: RPMh has \"%s\" at %#x; levels:%s\n", adreno8_step, name, address, levels);
}

static struct adreno8_block adreno8_qfprom = { "qfprom", 0x221c8000, 0x1000 };

/*
 * KGSL selects qcom,sku-codes from socinfo feature_code/pcode, separately from speed_bin.
 * Until socinfo is available, expose only the 125 MHz / 0x32 point shared by both K90 DT tables.
 */
static const struct adreno8_gpu_level adreno8_boot_speeds[] = {
	{ 125000000, 0x32 },
};

uint32_t adreno8_speed_bin;

const struct adreno8_gpu_level *adreno8_speeds;

unsigned adreno8_speeds_count;

/*
 * The message's tables start with a zero speed the GMU can idle into (Linux prepends the {0 Hz,
 * 0} operating point too), so gx_hz/gx_level[0] is that and 1..speeds_count the vendor's list.
 */

uint32_t adreno8_gx_hz[17], adreno8_gx_level[17];

unsigned adreno8_gx_count;

/* The GMU's own three speeds (dt: qcom,gmu-freq-table at the gmu node: Hz and cx.lvl level). */

const uint32_t adreno8_gmu_hz[3] = { 0, 500000000, 650000000 };

const uint32_t adreno8_gmu_level[3] = { 0, 0x40, 0x80 };

/* K90 DT qcom,bus-table-ddr, in KB/s. */
const uint32_t adreno8_bw_kb[BW_COUNT] = {
	0, 781250, 2136718, 5285156, 6074218, 6671875,
	8171875, 10687500, 12449218, 14398437, 16500000, 18597656,
};

/* SM8750 BCM resources; gen8.0.0 HFI performance mode starts at 2736 MHz DDR. */

const struct adreno8_bcm adreno8_bcms[] = {
	{ "SH0", 16, false, 0, 0 },
	{ "MC0",  4, false, 0, 0 },
	{ "ACV",  0, true,  1u << 2, 10687500 }, /* adreno_gen8_rpmh.c: 2736 * 4 * 1000000 / 1024 KB/s */
};

void
adreno8_show_rpmh_bcm(const char *name)
{
	const uint8_t *data = NULL;
	unsigned length = 0;
	uint32_t address = adreno8_rpmh_lookup(name, NULL, NULL, &data, &length);
	struct adreno8_bcm_db one;

	if (address == 0) {
		printf("adreno8: %u: RPMh has no \"%s\": the bandwidth table will be incomplete\n", adreno8_step, name);
		return;
	}
	if (length >= sizeof(one)) {
		memcpy(&one, data, sizeof(one));   /* little endian both sides */
		printf("adreno8: %u: RPMh has \"%s\" at %#x; monitor: width %u, vcd %u, unit %u\n", adreno8_step, name, address,
		    one.width, one.vcd, one.unit);
		return;
	}
	printf("adreno8: %u: RPMh has \"%s\" at %#x, %u bytes of description\n", adreno8_step, name, address, length);
}

/* The votes the messages will carry: one 32-bit word per speed (Linux: a6xx_perf_state populate). */

uint32_t adreno8_gx_votes[17], adreno8_gx_dep_votes[17], adreno8_cx_votes[3];

/*
 * One speed's supply vote: where this speed's level sits in its rail's list (first level at or
 * above it), and where in the neighbor rail's (first at or above, else the highest nonzero):
 * the two positions ride in the word's low bytes, the primary rail's value in its top. (Linux:
 * the pri/sec walk in a6xx_hfi's table build; sec is gmxc.lvl where it exists, mx.lvl where not.)
 */

bool
adreno8_arc_vote(const char *pri_n, const char *sec_n, uint32_t level, uint32_t *vote)
{
	uint16_t pri[RPMH_LEVELS], sec[RPMH_LEVELS];
	unsigned pc = 0, sc = 0, pindex, sindex = 0;

	if (adreno8_rpmh_lookup(pri_n, pri, &pc, NULL, NULL) == 0 || pc == 0) {
		printf("adreno8: %u: \"%s\" gives no levels: cannot vote\n", adreno8_step, pri_n);
		return false;
	}
	if (adreno8_rpmh_lookup(sec_n, sec, &sc, NULL, NULL) == 0 || sc == 0) {
		if (strcmp(sec_n, "gmxc.lvl") != 0) {
			printf("adreno8: %u: \"%s\" gives no levels: cannot vote\n", adreno8_step, sec_n);
			return false;
		}
		if (adreno8_rpmh_lookup("mx.lvl", sec, &sc, NULL, NULL) == 0 || sc == 0) {
			printf("adreno8: %u: neither \"gmxc.lvl\" nor \"mx.lvl\": cannot vote\n", adreno8_step);
			return false;
		}
	}
	for (pindex = 0; pindex < pc; pindex++) {
		if (pri[pindex] >= level) {
			break;
		}
	}
	if (pindex == pc) {
		printf("adreno8: %u: level %#x is above every level of \"%s\": the speed list and the rail do not agree\n", adreno8_step,
		    level, pri_n);
		return false;
	}
	for (unsigned j = 0; j < sc; j++) {
		if (sec[j] >= level) {
			sindex = j;
			break;
		}
		if (sec[j] != 0) {
			sindex = j;
		}
	}
	*vote = ((uint32_t)pri[pindex] << 16) | (sindex << 8) | pindex;
	return true;
}

/*
 * The dependency vote of one speed (Linux's dep loop): "while the 3D core runs at this speed the
 * memory rail must not fall below level X", told as a position in mx.lvl's list; 0xffffffff for
 * the idling zero speed.
 */

bool
adreno8_dep_vote(uint32_t level, uint32_t *vote)
{
	uint16_t mx[RPMH_LEVELS];
	unsigned count = 0, j;

	if (level == 0) {
		*vote = 0xffffffff;
		return true;
	}
	if (adreno8_rpmh_lookup("mx.lvl", mx, &count, NULL, NULL) == 0 || count == 0) {
		printf("adreno8: %u: no \"mx.lvl\": cannot say what the memory rail depends on\n", adreno8_step);
		return false;
	}
	for (j = 0; j < count; j++) {
		if (mx[j] >= level) {
			break;
		}
	}
	if (j == count) {
		printf("adreno8: %u: level %#x is above every level of \"mx.lvl\"\n", adreno8_step, level);
		return false;
	}
	*vote = (0x3fffu << 14) | (j << 8) | 0xff;
	return true;
}

bool
adreno8_step_outside(void)
{
	uint32_t bin;

	adreno8_announce("read the GPU speed bin at qfprom 0x221c8138, bits 8:0");
	/* SM8450 and later require word-aligned 32-bit QFPROM reads; see Linux qfprom.c. */
	bin = adreno8_get(&adreno8_qfprom, "speed bin", QFPROM_GPU_SPEED_BIN);
	adreno8_speed_bin = bin &= 0x1ff;
	adreno8_speeds = adreno8_boot_speeds;
	adreno8_speeds_count = sizeof(adreno8_boot_speeds) / sizeof(adreno8_boot_speeds[0]);
	printf("adreno8: %u: speed bin %u; SKU unavailable, limiting GX to the common 125 MHz point\n", adreno8_step, bin);
	adreno8_gx_count = 1;
	adreno8_gx_hz[0] = 0;
	adreno8_gx_level[0] = 0;                        /* the idling speed the message tables start with (Linux prepends the 0 Hz operating point) */
	for (unsigned i = 0; i < adreno8_speeds_count; i++) {
		adreno8_gx_hz[adreno8_gx_count] = adreno8_speeds[i].hz;
		adreno8_gx_level[adreno8_gx_count] = adreno8_speeds[i].level;
		adreno8_gx_count++;
	}
	printf("adreno8: %u: bin %u: the 3D core has %u speeds, to %u Hz; levels %#x..%#x\n", adreno8_step, bin, adreno8_speeds_count,
	    adreno8_speeds[adreno8_speeds_count - 1].hz, adreno8_speeds[0].level, adreno8_speeds[adreno8_speeds_count - 1].level);

	adreno8_announce("read the GPU's memory clock in the global clock controller; its register-bus clock (gcc_gpu_cfg_ahb) is one of the hypervisor's PROTECTED clocks here: no offset, no driver, nothing to read or write -- the loader runs it and step 2 will show whether registers answer");
	adreno8_get(&adreno8_gcc, "ddrss_gpu_axi", GCC_DDRSS_GPU_AXI);

	adreno8_announce("look up the GPU's supplies and bus monitors in the boot loader's list of what RPMh can switch (memory at 0x81c60000)");
	adreno8_show_rpmh_resource("gfx.lvl");
	adreno8_show_rpmh_resource("cx.lvl");
	adreno8_show_rpmh_resource("mx.lvl");
	adreno8_show_rpmh_resource("gmxc.lvl");
	for (unsigned i = 0; i < BCM_COUNT; i++) {
		adreno8_show_rpmh_bcm(adreno8_bcms[i].name);
	}
	adreno8_show_rpmh_bcm("CN0");
	if (adreno8_cmd_db == NULL) {
		printf("adreno8: %u: no list to read messages from: steps 13 on will fail\n", adreno8_step);
	}
	return true;
}

/* ---- step 2: the GPU's clock controller, as left ------------------------------------------------------- */

/*
 * dt: clock-controller@3d90000. The register list is drivers/clk/qcom/gpucc-sm8750.c: one power
 * switch (CX; there is no GX one for the OS to pull: the 3D core's power is the GMU firmware's
 * to give, through its PWR_CLK), thirteen branches, two clock sources over one PLL. TAYCAN_ELU,
 * the PLL, is a known set of values at unverified offsets on this SoC, so it is read, not
 * written: the GMU is left on the crystal's rate, which its firmware runs at anyway (the
 * "gmu clk" vote in the firmware's HFI protocol starts at TCXO). (TODO: PLL offsets.)
 */

struct adreno8_block adreno8_gpucc = { "gpucc", 0x03d90000, 0x10000 };

bool
adreno8_gpucc_answers(void)
{
	uint32_t value;

	if (adreno8_step != adreno8_newest_step) {
		adreno8_need(&adreno8_gpucc);
		return adreno8_rd(adreno8_gpucc.base + GPUCC_CX_GDSCR) != 0xffffffff;
	}
	value = adreno8_get(&adreno8_gpucc, "cx gdscr", GPUCC_CX_GDSCR);
	return value != 0xffffffff;
}

bool
adreno8_cx_is_on(void)
{
	adreno8_need(&adreno8_gpucc);
	return (adreno8_rd(adreno8_gpucc.base + GPUCC_CX_GDSCR) | adreno8_rd(adreno8_gpucc.base + GPUCC_CX_HW_CTRL)) & GDSC_POWER_ON;
}

bool
adreno8_step_gpucc(void)
{
	adreno8_announce("read the GPU's clock controller as the boot loader left it: power switch, thirteen branches, two clock sources; the PLL area read only, its offsets are unverified (gpucc-sm8750.c)");
	if (!adreno8_gpucc_answers()) {
		printf("adreno8: %u: the GPU's clock controller does not answer (all ones)\n", adreno8_step);
		return false;
	}
	adreno8_get(&adreno8_gpucc, "cx hw control", GPUCC_CX_HW_CTRL);
	for (unsigned i = 0; i < 8; i++) {
		adreno8_get(&adreno8_gpucc, "pll0 area, read only", 4 * i);
	}
	adreno8_get(&adreno8_gpucc, "ahb", GPUCC_AHB_CBCR);
	adreno8_get(&adreno8_gpucc, "cxo", GPUCC_CX_CBCR);
	adreno8_get(&adreno8_gpucc, "cx_gmu", GPUCC_CX_GMU_CBCR);
	adreno8_get(&adreno8_gpucc, "gx_gmu", GPUCC_GX_GMU_CBCR);
	adreno8_get(&adreno8_gpucc, "gx_accu_shift", GPUCC_GX_ACCU_CBCR);
	adreno8_get(&adreno8_gpucc, "cx_accu_shift", GPUCC_CX_ACCU_CBCR);
	adreno8_get(&adreno8_gpucc, "demet", GPUCC_DEMET_CBCR);
	adreno8_get(&adreno8_gpucc, "dpm", GPUCC_DPM_CBCR);
	adreno8_get(&adreno8_gpucc, "freq_measure", GPUCC_FREQ_MEASURE);
	adreno8_get(&adreno8_gpucc, "hlos1_vote_gpu_smmu", GPUCC_SMMU_VOTE_CBCR);
	adreno8_get(&adreno8_gpucc, "memnoc_gfx", GPUCC_MEMNOC_GFX_CBCR);
	adreno8_get(&adreno8_gpucc, "hub_aon", GPUCC_HUB_AON_CBCR);
	adreno8_get(&adreno8_gpucc, "hub_cx_int", GPUCC_HUB_CX_INT_CBCR);
	adreno8_get(&adreno8_gpucc, "gpu_cc_gmu bcr", GPUCC_GMU_BCR);
	adreno8_get(&adreno8_gpucc, "gmu_clk_src command", GPUCC_GMU_RCGR);
	adreno8_get(&adreno8_gpucc, "gmu_clk_src source", GPUCC_GMU_RCGR + 4);
	adreno8_get(&adreno8_gpucc, "hub_clk_src command", GPUCC_HUB_RCGR);
	adreno8_get(&adreno8_gpucc, "hub_clk_src source", GPUCC_HUB_RCGR + 4);
	adreno8_get(&adreno8_gpucc, "hub_div", GPUCC_HUB_DIV);
	return true;
}

/* ---- steps 3 and 5's helpers --------------------------------------------------------------------------- */

bool adreno8_cx_changed;
uint32_t adreno8_cx_saved_gdscr;

bool
adreno8_clock_on(struct adreno8_block *b, const char *name, uint32_t cbcr, bool must)
{
	if (!(adreno8_set(b, name, cbcr, CBCR_ENABLE) & CBCR_OFF) || adreno8_wait_for(b, cbcr, CBCR_OFF, 0, 10)) {
		return true;
	}
	printf("adreno8: %u: %s %s stays off (%#x)%s\n", adreno8_step, b->name, name, adreno8_rd(b->base + cbcr),
	    must ? "" : "; its status bit is not one to rely on, going on");
	return !must;
}

bool
adreno8_clock_source(const char *name, uint32_t rcgr, uint32_t source)
{
	uint32_t was = adreno8_get(&adreno8_gpucc, name, rcgr + 4);

	if ((was & RCG_SOURCE_MASK) == source) {
		return true;
	}
	adreno8_put(&adreno8_gpucc, name, rcgr + 4, (was & ~RCG_SOURCE_MASK) | source);
	adreno8_put(&adreno8_gpucc, name, rcgr, adreno8_rd(adreno8_gpucc.base + rcgr) | RCG_UPDATE);
	if (!adreno8_wait_for(&adreno8_gpucc, rcgr, RCG_UPDATE, 0, 10)) {
		printf("adreno8: %u: %s does not take its new source (command %#x)\n", adreno8_step, name, adreno8_rd(adreno8_gpucc.base + rcgr));
		return false;
	}
	adreno8_get(&adreno8_gpucc, name, rcgr + 4);
	return true;
}

/*
 * What later steps check before touching anything: the CX half answered, is up, and has the two
 * clocks its registers live on. This machine has no ported frequency counter (redwood measured
 * with debugcc; SM8750's counterpart is unmapped here), so the control registers are trusted and
 * the next step's first read shows whether they lied.
 */

bool
adreno8_cx_ready(void)
{
	uint32_t cxo, adreno8_gmu;

	if (!adreno8_gpucc_answers()) {
		printf("adreno8: %u: the GPU's clock controller does not answer: step 2\n", adreno8_step);
		return false;
	}
	if (!adreno8_cx_is_on()) {
		printf("adreno8: %u: CX is off: step 3\n", adreno8_step);
		return false;
	}
	adreno8_need(&adreno8_gpucc);
	cxo = adreno8_rd(adreno8_gpucc.base + GPUCC_CX_CBCR);
	adreno8_gmu = adreno8_rd(adreno8_gpucc.base + GPUCC_CX_GMU_CBCR);
	if (!(cxo & CBCR_ENABLE) || cxo & CBCR_OFF) {
		printf("adreno8: %u: the crystal's clock into the GPU block is not running (%#x): step 5\n", adreno8_step, cxo);
		return false;
	}
	if (!(adreno8_gmu & CBCR_ENABLE) || adreno8_gmu & CBCR_OFF) {
		printf("adreno8: %u: the GMU's clock is not running (%#x): step 5\n", adreno8_step, adreno8_gmu);
		return false;
	}
	return true;
}

/* ---- step 3: the CX half on ------------------------------------------------------------------------------ */

bool
adreno8_step_cx_power(void)
{
	uint32_t gdscr, hw;
	uint64_t start, waited = 0;

	adreno8_announce("read the GPU clock controller's CX power switch: on this SoC the 3D core has no switch for the OS to pull -- its power comes from the GMU's firmware (steps 11 to 14); only the CX half is ours");
	if (!adreno8_gpucc_answers()) {
		printf("adreno8: %u: the GPU's clock controller does not answer: step 2\n", adreno8_step);
		return false;
	}
	gdscr = adreno8_get(&adreno8_gpucc, "cx gdscr", GPUCC_CX_GDSCR);
	hw = adreno8_get(&adreno8_gpucc, "cx hw control", GPUCC_CX_HW_CTRL);
	if ((gdscr | hw) & GDSC_POWER_ON) {
		printf("adreno8: %u: THE CX HALF IS UP ALREADY: the boot loader (or an earlier step in this session) switched it on and the AOP keeps it: leaving it and going on (unlike redwood: on this phone the vendor's preemption logic wants CX up)\n", adreno8_step);
		if (adreno8_speeds == NULL) {
			printf("adreno8: %u: (speed bin not read this session: step 1 alone asks the fuses)\n", adreno8_step);
		}
		return true;
	}
	if (gdscr & GDSC_HW_CONTROL) {
		printf("adreno8: %u: the CX switch is under hardware control and off (%#x): it is not ours to pull\n", adreno8_step, gdscr);
		return false;
	}
	adreno8_announce("ask the CX half up (gpucc cx_gdscr bit 0: collapse off) and time how long it takes: half a second at most");
	/* Restore the full configuration even if the power-on poll fails. */
	adreno8_cx_saved_gdscr = gdscr;
	adreno8_cx_changed = true;
	adreno8_put(&adreno8_gpucc, "cx gdscr", GPUCC_CX_GDSCR, gdscr & ~GDSC_SW_COLLAPSE);
	start = mach_absolute_time();
	for (int i = 0; i < 5000 && !adreno8_cx_is_on(); i++) {
		adreno8_sleep_us(100);
	}
	absolutetime_to_nanoseconds(mach_absolute_time() - start, &waited);
	adreno8_get(&adreno8_gpucc, "cx gdscr", GPUCC_CX_GDSCR);
	adreno8_get(&adreno8_gpucc, "cx hw control", GPUCC_CX_HW_CTRL);
	if (!adreno8_cx_is_on()) {
		printf("adreno8: %u: the CX half stayed down after %llu us: it does not answer\n", adreno8_step, waited / 1000);
		return false;
	}
	printf("adreno8: %u: THE CX HALF IS UP: %llu us after the write\n", adreno8_step, waited / 1000);
	return true;
}

/* ---- step 4: that it is awake ---------------------------------------------------------------------------- */

/*
 * dt: cx_misc@3d9e000 and rscc@3d50000. TCM_RET_CNTL reports retained GMU memory.
 * RSCC registers use dword indices; the status register reports whether the RSC is asleep.
 */

struct adreno8_block adreno8_cx_misc = { "cx_misc", 0x03d9e000, 0x2000 };

struct adreno8_block adreno8_rscc = { "rscc", 0x03d50000, 0x10000 };

static struct adreno8_block adreno8_gbif = { "gbif", 0x03d0f000, 0x1000 };

bool adreno8_rsc_asleep_at_first;

bool
adreno8_step_cx_awake(void)
{
	uint32_t tcm, status;

	if (!adreno8_cx_ready()) {
		return false;
	}
	adreno8_announce("read CX memory retention, feature fuses and bus halt state");
	tcm = adreno8_get(&adreno8_cx_misc, "tcm retention", CX_MISC_TCM_RET);
	printf("adreno8: %u: the GMU's memories were %s: a %s start\n", adreno8_step, tcm == 1 ? "kept" : "not kept",
	    tcm == 1 ? "warm" : "cold");
	adreno8_get(&adreno8_cx_misc, "sw fuse", CX_MISC_SW_FUSE);
	adreno8_get(&adreno8_gbif, "halt", GBIF_HALT);
	adreno8_get(&adreno8_gbif, "halt ack", GBIF_HALT_ACK);
	printf("adreno8: %u: CX registers answer\n", adreno8_step);

	adreno8_announce("read RSCC sleep and sequence status");
	adreno8_get(&adreno8_rscc, "drv ctrl", RSCC_DRV_CTRL);
	status = adreno8_get(&adreno8_rscc, "rsc status", RSCC_RSC_STATUS);
	adreno8_rsc_asleep_at_first = (status & RSCC_ASLEEP) != 0;
	adreno8_get(&adreno8_rscc, "seq busy drv0", RSCC_SEQ_BUSY);
	printf("adreno8: %u: the RSC itself is %s\n", adreno8_step, adreno8_rsc_asleep_at_first ? "asleep: step 11 wakes it first" : "awake");

	return true;
}

/* ---- step 5: its clocks ---------------------------------------------------------------------------------- */

bool
adreno8_step_clocks(void)
{
	if (!adreno8_gpucc_answers()) {
		return false;
	}
	if (!adreno8_cx_is_on()) {
		printf("adreno8: %u: CX is off: step 3\n", adreno8_step);
		return false;
	}
	adreno8_announce("in the global clock controller: the GPU's memory bus tap on (gcc 0x71150; its halt report is unreliable: tolerated); the register-bus clock of the GPU controller is the hypervisor's protected one: nothing to write");
	if (!adreno8_clock_on(&adreno8_gcc, "ddrss_gpu_axi", GCC_DDRSS_GPU_AXI, false)) {
		return false;
	}

	adreno8_announce("in the GPU's clock controller: the GMU's clock source to the crystal (parent 0; adreno8_gmuclk=1 would ask PLL0_OUT_EVEN, parent 2, whose PLL offsets are unverified: see the header)");
	if (!adreno8_clock_source("gmu_clk_src", GPUCC_GMU_RCGR, adreno8_gmu_from_pll ? (2u << 8) | 1u : 0)) {
		return false;
	}
	if (adreno8_gmu_from_pll) {
		adreno8_announce("the PLL area again, now that a vote for it is out (offsets unverified: read for the record)");
		for (unsigned i = 0; i < 8; i++) {
			adreno8_get(&adreno8_gpucc, "pll0 area, read only", 4 * i);
		}
	}
	adreno8_announce("in the GPU's clock controller: the clocks of the GMU and of the way in on; the hub's source is left as the loader set it (its parents are unverified); the ones whose halt report lies are tolerated");
	if (!adreno8_clock_on(&adreno8_gpucc, "cxo", GPUCC_CX_CBCR, true) ||
	    !adreno8_clock_on(&adreno8_gpucc, "cx_gmu", GPUCC_CX_GMU_CBCR, true) ||
	    !adreno8_clock_on(&adreno8_gpucc, "ahb", GPUCC_AHB_CBCR, false) ||
	    !adreno8_clock_on(&adreno8_gpucc, "gx_gmu", GPUCC_GX_GMU_CBCR, false) ||
	    !adreno8_clock_on(&adreno8_gpucc, "gx_accu_shift", GPUCC_GX_ACCU_CBCR, false) ||
	    !adreno8_clock_on(&adreno8_gpucc, "cx_accu_shift", GPUCC_CX_ACCU_CBCR, false) ||
	    !adreno8_clock_on(&adreno8_gpucc, "demet", GPUCC_DEMET_CBCR, false) ||
	    !adreno8_clock_on(&adreno8_gpucc, "dpm", GPUCC_DPM_CBCR, false) ||
	    !adreno8_clock_on(&adreno8_gpucc, "hlos1_vote_gpu_smmu", GPUCC_SMMU_VOTE_CBCR, false) ||
	    !adreno8_clock_on(&adreno8_gpucc, "memnoc_gfx", GPUCC_MEMNOC_GFX_CBCR, false) ||
	    !adreno8_clock_on(&adreno8_gpucc, "hub_aon", GPUCC_HUB_AON_CBCR, false) ||
	    !adreno8_clock_on(&adreno8_gpucc, "hub_cx_int", GPUCC_HUB_CX_INT_CBCR, false)) {
		return false;
	}
	adreno8_announce("read every one of them back, and the GMU's clock source with it");
	adreno8_get(&adreno8_gpucc, "gmu_clk_src source", GPUCC_GMU_RCGR + 4);
	adreno8_get(&adreno8_gpucc, "cxo", GPUCC_CX_CBCR);
	adreno8_get(&adreno8_gpucc, "cx_gmu", GPUCC_CX_GMU_CBCR);
	adreno8_get(&adreno8_gpucc, "gpu_cc_gmu bcr", GPUCC_GMU_BCR);
	adreno8_get(&adreno8_gpucc, "ahb", GPUCC_AHB_CBCR);
	return true;
}

/* Qualcomm standard SMC64 with the A6 interrupted-call continuation convention. */
int32_t
adreno8_scm_call(uint64_t function, uint64_t arginfo, uint64_t argument0,
    uint64_t argument1, uint64_t argument2, uint64_t argument3, uint64_t *answer)
{
	uint64_t session = 0;

	adreno8_announce("SCM call %#llx arguments %#llx %#llx %#llx", function, argument0, argument1, argument2);
	for (unsigned attempt = 0; attempt < 100; attempt++) {
		register uint64_t x0 __asm__("x0") = attempt == 0 ? function : 1;
		register uint64_t x1 __asm__("x1") = arginfo;
		register uint64_t x2 __asm__("x2") = argument0;
		register uint64_t x3 __asm__("x3") = argument1;
		register uint64_t x4 __asm__("x4") = argument2;
		register uint64_t x5 __asm__("x5") = argument3;
		register uint64_t x6 __asm__("x6") = session;
		register uint64_t x7 __asm__("x7") = 0;

		__asm__ volatile ("smc #0" : "+r" (x0), "+r" (x1), "+r" (x2), "+r" (x3), "+r" (x4), "+r" (x5),
		    "+r" (x6), "+r" (x7) : : "x8", "x9", "x10", "x11", "x12", "x13", "x14", "x15", "x16", "x17", "memory");
		session = x6;
		if (x0 != 1) {
			*answer = x1;
			printf("adreno8: %u: SCM %#llx returned %d, answer %#llx\n", adreno8_step, function, (int32_t)x0, x1);
			return (int32_t)x0;
		}
	}
	printf("adreno8: %u: SCM continuation limit reached\n", adreno8_step);
	return -1;
}

bool
adreno8_scm_gpu_init(void)
{
	uint64_t answer = 0;
	int32_t status = adreno8_scm_call(0x42000601, 1, 0x02002801, 0, 0, 0, &answer);

	/* qcom_scm_kgsl_init_regs permits EOPNOTSUPP; all other errors stop this probe. */
	if (status == -4 || (status == 0 && answer == 0)) {
		printf("adreno8: %u: SCM GPU register initialization unavailable\n", adreno8_step);
		return true;
	}
	if (status != 0 || answer != 1) {
		return false;
	}
	/* kgsl_util.h: mandatory register access and TSENSE IDs; BCL and CLX are disabled. */
	status = adreno8_scm_call(0x42002801, 1, 0x9, 0, 0, 0, &answer);
	return status == 0 || status == -4;
}
