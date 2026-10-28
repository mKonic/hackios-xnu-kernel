/*
 * pcie_power=1 (with pcie=2 or more): power up PCIe root complex 0 and the WCN7850 behind it when
 * mhi_pcie.c's step 1 finds them off, as the boot firmware leaves them
 * (docs/progress/2026-10-26-wifi-pcie-survey.md). The vendor kernel brings these resources up through
 * CESTA and the PCIe state manager; mainline Linux does not, and neither does this file. The order and
 * the values follow mainline -- pcie-qcom.c's cfg_1_9_0 (sm8750 falls back to "qcom,pcie-sm8550"),
 * phy-qcom-qmp-pcie.c's sm8750 gen3x2 PHY, gcc-sm8750.c, tcsrcc-sm8750.c, pwrseq-qcom-wcn.c,
 * sm8750.dtsi and sm8750-mtp.dts -- with this phone's supplies and PHY values from the vendor device
 * tree. The ordered list with the source of each step is the local evidence file
 * wifi-pcie-plan/powerup-steps.txt; the letters below are its sections.
 *
 *   A  read only: GPLL0 locked; the eleven RPMh resources found in the command database, each tagged
 *      as a regulator ("VRM")
 *   B  PERST (GPIO102) and the card's enable (GPIO16) driven low before any supply
 *   C  RPMh votes: the PHY's two supplies, the card's eight, the card's crystal (rfclka1)
 *   D  clock controller: power domains, aux at 19.2 MHz and rchng at 100 MHz, the reference clock, the
 *      branches
 *   E  core and PHY resets
 *   F  PARF, the controller's glue
 *   G  the PHY sequence, the pipe clock, PHY start, PHYSTATUS
 *   H  the card's enable, then CLKREQ and WAKE to their functions
 *   I  the root port's own header, the link capped at Gen1 for this first batch
 *   J  PERST released 100 ms after the card's enable, link training, the link awaited
 *
 * Every voltage is checked against the intersection of the regulator's range and the card's range in
 * the vendor device tree before it is sent; a vote outside it is refused, not sent. RPMh takes the
 * highest vote of all processors, so a vote of ours cannot take a shared rail below another's.
 * Nothing is undone at the end: the run ends in a reset.
 */
#include <kern/clock.h>
#include <libkern/libkern.h>
#include <machine/machine_routines.h>
#include <sys/systm.h>

#include "mhi_pcie_private.h"
#include "mhi_pcie_phy_seq.h"

#define printf mhi_pcie_say
#define rd mhi_pcie_rd
#define wr mhi_pcie_wr

extern vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);
extern vm_offset_t ml_io_map_wcomb(vm_offset_t phys_addr, vm_size_t size);

static const struct mhi_pcie_windows *w;
static vm_offset_t rsc, cmd_db, tcsr;

static void
sleep_us(uint32_t us)
{
	delay_for_interval(us, NSEC_PER_USEC);
}

/* Wait until (register & mask) == value, polling every step_us for at most limit_us. */
static bool
poll(vm_offset_t reg, uint32_t mask, uint32_t value, uint32_t step_us, uint32_t limit_us, const char *what)
{
	for (uint32_t waited = 0;; waited += step_us) {
		if ((rd(reg) & mask) == value) {
			return true;
		}
		if (waited >= limit_us) {
			printf("pcie: power: %s did not happen in %u us (%#010x)\n", what, limit_us, rd(reg));
			return false;
		}
		sleep_us(step_us);
	}
}

/* ---- C: RPMh ---------------------------------------------------------------------------------- */

#define RSC_PHYS                0x16520000UL        /* apps RSC, DRV2 (the same one redwood_goodix.c uses) */
#define RSC_SIZE                0x10000UL
#define CMD_DB_PHYS             0x81c60000UL        /* aop_cmd_db_region, as redwood_goodix.c's board_setup() */
#define CMD_DB_SIZE             0x20000UL

/*
 * DRV2's qcom,tcs-config lists three ACTIVE sets first, so slot sets 0-2 are active ones; the
 * touch driver uses set 0 and adreno8_rpmh.c set 1, this file set 2. Layout of RSC v3 (rpmh-rsc.c):
 * the interrupt registers at 0xd00 hold one bit per set; each set is 0x2a0 long from 0xd00.
 */
#define DRV_IRQ_ENABLE          0xd00
#define DRV_IRQ_STATUS          0xd04
#define DRV_IRQ_CLEAR           0xd08
#define TCS_BIT                 (1u << 2)
#define TCS_BASE                (0xd00 + 2 * 0x2a0)
#define TCS_WAIT                (TCS_BASE + 0x20)
#define TCS_CONTROL             (TCS_BASE + 0x24)
#define TCS_STATUS              (TCS_BASE + 0x28)    /* not 0 when idle (rpmh-rsc.c tcs_is_free) */
#define TCS_CMD_ENABLE          (TCS_BASE + 0x2c)
#define TCS_MSGID(n)            (TCS_BASE + 0x34 + 24 * (n))
#define TCS_ADDR(n)             (TCS_MSGID(n) + 4)
#define TCS_DATA(n)             (TCS_MSGID(n) + 8)
#define TCS_CONTROL_ENABLE      (1u << 16)
#define TCS_CONTROL_TRIGGER     (1u << 24)
#define MSGID_WRITE_RESPONSE    0x10108             /* write, 4 bytes, response requested */

#define VRM_VOLTAGE             0                   /* millivolts */
#define VRM_ENABLE              4
#define VRM_MODE                8
#define VRM_MODE_LDO_HPM        7
#define CMD_DB_VRM              4                   /* the slave id in bits 19:16 of a regulator's address */

struct cmd_db_slave {
	uint16_t id, header_offset, data_offset, count, version, reserved[3];
};
struct cmd_db_header {
	uint32_t version;
	uint8_t magic[4];
	struct cmd_db_slave slave[8];
	uint32_t checksum, reserved;
	uint8_t data[];
};
struct cmd_db_entry {
	char id[8];
	uint32_t priority[2];
	uint32_t address;
	uint16_t length, offset;
};

static uint32_t
rpmh_address(const char *name)
{
	const struct cmd_db_header *db = (const struct cmd_db_header *)cmd_db;
	char id[8] = { 0 };

	if (db->magic[0] != 0xdb || db->magic[1] != 0x30 || db->magic[2] != 0x03 || db->magic[3] != 0x0c) {
		return 0;
	}
	strlcpy(id, name, sizeof(id));
	for (unsigned s = 0; s < 8; s++) {
		const struct cmd_db_slave *slave = &db->slave[s];
		const struct cmd_db_entry *entry = (const struct cmd_db_entry *)(db->data + slave->header_offset);

		if (slave->id == 0 || slave->header_offset >= CMD_DB_SIZE - 0x1000 || slave->count > 512) {
			continue;
		}
		for (unsigned i = 0; i < slave->count; i++) {
			if (memcmp(entry[i].id, id, sizeof(id)) == 0) {
				return entry[i].address;
			}
		}
	}
	return 0;
}

/* Up to three writes in one transaction on slot set 2, waited for. The shared interrupt registers are
 * changed only in this set's bit. */
static bool
rpmh_write(const uint32_t *address, const uint32_t *data, unsigned count)
{
	uint32_t mask = (1u << count) - 1;
	bool done;

	if (rd(rsc + TCS_STATUS) == 0 || (rd(rsc + TCS_CONTROL) & TCS_CONTROL_TRIGGER) != 0) {
		printf("pcie: power: RPMh slot set 2 is busy (status %#x, control %#x)\n", rd(rsc + TCS_STATUS),
		    rd(rsc + TCS_CONTROL));
		return false;
	}
	wr(rsc + TCS_CONTROL, 0);
	wr(rsc + DRV_IRQ_CLEAR, TCS_BIT);
	for (unsigned i = 0; i < count; i++) {
		wr(rsc + TCS_MSGID(i), MSGID_WRITE_RESPONSE);
		wr(rsc + TCS_ADDR(i), address[i]);
		wr(rsc + TCS_DATA(i), data[i]);
	}
	wr(rsc + TCS_WAIT, mask);
	wr(rsc + TCS_CMD_ENABLE, mask);
	wr(rsc + DRV_IRQ_ENABLE, rd(rsc + DRV_IRQ_ENABLE) | TCS_BIT);
	wr(rsc + TCS_CONTROL, TCS_CONTROL_ENABLE);
	wr(rsc + TCS_CONTROL, TCS_CONTROL_ENABLE | TCS_CONTROL_TRIGGER);
	done = poll(rsc + DRV_IRQ_STATUS, TCS_BIT, TCS_BIT, 100, 200000, "the RPMh request's completion");
	wr(rsc + TCS_CONTROL, 0);
	wr(rsc + TCS_CMD_ENABLE, 0);
	wr(rsc + TCS_WAIT, 0);
	wr(rsc + DRV_IRQ_CLEAR, TCS_BIT);
	wr(rsc + DRV_IRQ_ENABLE, rd(rsc + DRV_IRQ_ENABLE) & ~TCS_BIT);
	return done;
}

/*
 * The supplies, in the order the vendor's cnss driver turns the card's on (io, io12, ant-share, vdd,
 * aon, dig, rfa1, rfa2), after the PHY's two. min/max: the regulator's range in the vendor device
 * tree intersected with the card's (qcom,vdd-wlan-*-config) or the PHY's (sm8750-mtp.dts), microvolts.
 * smpd1's 976 mV is the regulator's init voltage, inside the card's 916-1100 mV.
 */
static const struct supply {
	const char *name, *what;
	uint32_t mv, min_uv, max_uv;
	bool ldo;
} supplies[] = {
	{ "ldof1", "PHY vdda-phy (L1F)", 880, 880000, 920000, true },
	{ "ldog3", "PHY vdda-pll (L3G)", 1200, 1200000, 1256000, true },
	{ "ldof3", "card vdd-wlan-io (L3F)", 1800, 1800000, 1800000, true },
	{ "ldof2", "card vdd-wlan-io12 (L2F)", 1200, 1200000, 1200000, true },
	{ "ldok6", "card vdd-wlan-ant-share (L6K)", 1800, 1800000, 1800000, true },
	{ "smpf5", "card vdd-wlan (S5F)", 932, 932000, 1000000, false },
	{ "smpd4", "card vdd-wlan-aon (S4D)", 976, 976000, 1036000, false },
	{ "smpd1", "card vdd-wlan-dig (S1D)", 976, 916000, 1100000, false },
	{ "smpg3", "card vdd-wlan-rfa1 (S3G)", 1864, 1864000, 2000000, false },
	{ "smpi7", "card vdd-wlan-rfa2 (S7I)", 1316, 1316000, 1340000, false },
};
#define XO_NAME                 "rfclka1"           /* RPMH_RF_CLK1, the card's crystal (clk-rpmh.c) */
#define VRM_CLOCK_ENABLE        4

static bool
supplies_found(uint32_t *found, uint32_t *xo)
{
	bool all = true;

	for (unsigned i = 0; i < sizeof(supplies) / sizeof(supplies[0]); i++) {
		found[i] = rpmh_address(supplies[i].name);
		printf("pcie: power: %s for the %s at RPMh %#x\n", supplies[i].name, supplies[i].what, found[i]);
		if (found[i] == 0 || (found[i] >> 16 & 0xf) != CMD_DB_VRM) {
			all = false;
		}
	}
	*xo = rpmh_address(XO_NAME);
	printf("pcie: power: %s for the card's crystal at RPMh %#x\n", XO_NAME, *xo);
	if (*xo == 0 || (*xo >> 16 & 0xf) != CMD_DB_VRM) {
		all = false;
	}
	if (!all) {
		printf("pcie: power: a resource is missing or not tagged as a regulator; nothing is voted\n");
	}
	return all;
}

static bool
supplies_on(const uint32_t *found, uint32_t xo)
{
	for (unsigned i = 0; i < sizeof(supplies) / sizeof(supplies[0]); i++) {
		const struct supply *s = &supplies[i];
		uint32_t address[3] = { found[i] + VRM_VOLTAGE, found[i] + VRM_ENABLE, found[i] + VRM_MODE };
		uint32_t data[3] = { s->mv, 1, VRM_MODE_LDO_HPM };

		if (s->mv * 1000 < s->min_uv || s->mv * 1000 > s->max_uv) {
			printf("pcie: power: %u mV for %s is outside %u-%u uV; refused\n", s->mv, s->name, s->min_uv, s->max_uv);
			return false;
		}
		if (!rpmh_write(address, data, s->ldo ? 3 : 2)) {
			printf("pcie: power: the vote for %s was not confirmed\n", s->name);
			return false;
		}
		printf("pcie: power: %s on at %u mV\n", s->name, s->mv);
	}
	{
		uint32_t address = xo + VRM_CLOCK_ENABLE, data = 1;

		if (!rpmh_write(&address, &data, 1)) {
			printf("pcie: power: the vote for %s was not confirmed\n", XO_NAME);
			return false;
		}
	}
	printf("pcie: power: the card's crystal is on\n");
	return true;
}

/* ---- B, H, J: pins ------------------------------------------------------------------------------ */

#define PIN_CTL(p)              (w->tlmm + 0x1000UL * (p))
#define PIN_IO(p)               (PIN_CTL(p) + 4)
#define PIN_IO_OUT_HIGH         (1u << 1)
#define PIN_WLAN_EN             16
#define PIN_PERST               102
#define PIN_CLKREQ              103
#define PIN_WAKE                104
#define CTL_OUT_LOW_2MA         0x201               /* pull-down, gpio, 2 mA, output enable */
#define CTL_OUT_LOW_8MA         0x2c1               /* pull-down, gpio, 8 mA, output enable */
#define CTL_CLKREQ_FUNCTION     0x007               /* pull-up, function 1 (pcie0_clk_req_n) */
#define CTL_IN_PULL_UP          0x003

/* ---- D, E, G: clock controller and resets (gcc-sm8750.c offsets) -------------------------------- */

#define GCC_GPLL0_MODE          0x00000             /* bit 31: locked */
#define GCC_RSCC_VOTE           0x52010             /* bits 20, 21: pcie_rscc cfg_ahb and xo */
#define GCC_BRANCH_VOTE         0x52008             /* bits 0-6: pcie_0 slv_axi, mstr_axi, cfg_ahb, aux, pipe, slv_q2a, cnoc_pcie_sf */
#define GCC_BRANCH_VOTE2        0x52000             /* bits 12, 19, 22: aggre_noc_pcie, ddrss_pcie_sf_qtb, pcie_0_phy_rchng */
#define GCC_COLLAPSE_VOTE       0x5214c             /* bit 0 pcie_0_gdsc, bit 2 pcie_0_phy_gdsc: 1 collapses */
#define GCC_GDSC_CORE           0x6b004
#define GCC_GDSC_PHY            0x6c000
#define GDSC_SW_COLLAPSE        (1u << 0)
#define GDSC_POWER_UP_COMPLETE  (1u << 16)          /* in the CFG register right after the GDSCR */
#define GCC_PCIE0_BCR           0x6b000
#define GCC_PCIE0_PHY_BCR       0x6c01c
#define GCC_AUX_RCG             0x6b084             /* CMD; CFG follows */
#define GCC_RCHNG_RCG           0x6b068
#define GCC_PIPE_MUX            0x6b080             /* [1:0] 0: the PHY's pipe clock */
#define RCG_UPDATE              (1u << 0)
#define RCG_ROOT_EN             (1u << 1)
#define RCG_ROOT_OFF            (1u << 31)
#define CBCR_CLK_OFF            (1u << 31)
#define TCSR_CLKREF_PHYS        0x0f204000UL        /* TCSR_PCIE_0_CLKREF_EN at +8, bit 0 */
#define TCSR_CLKREF_EN          0x8

static bool
gdsc_on(uint32_t gdscr, uint32_t vote_bit, const char *name)
{
	vm_offset_t g = w->gcc;

	wr(g + GCC_COLLAPSE_VOTE, rd(g + GCC_COLLAPSE_VOTE) & ~vote_bit);
	if (poll(g + gdscr + 4, GDSC_POWER_UP_COMPLETE, GDSC_POWER_UP_COMPLETE, 50, 2000, name)) {
		return true;
	}
	/* (inferred, not in mainline for this domain) the domain's own switch, if the vote alone did not do it */
	wr(g + gdscr, rd(g + gdscr) & ~GDSC_SW_COLLAPSE);
	return poll(g + gdscr + 4, GDSC_POWER_UP_COMPLETE, GDSC_POWER_UP_COMPLETE, 50, 2000, name);
}

static bool
rcg_set(uint32_t cmd, uint32_t cfg, const char *name)
{
	vm_offset_t g = w->gcc;

	wr(g + cmd, rd(g + cmd) | RCG_ROOT_EN);
	if (!poll(g + cmd, RCG_ROOT_OFF, 0, 10, 500, name)) {
		return false;
	}
	wr(g + cmd + 4, cfg);
	wr(g + cmd, rd(g + cmd) | RCG_UPDATE);
	if (!poll(g + cmd, RCG_UPDATE, 0, 10, 500, name)) {
		return false;
	}
	wr(g + cmd, rd(g + cmd) & ~RCG_ROOT_EN);
	return true;
}

static bool
clocks_on(void)
{
	static const uint32_t branches[] = { 0x6b020, 0x6b040, 0x6b044, 0x6b01c, 0x6b064, 0x10058 };
	vm_offset_t g = w->gcc;

	wr(g + GCC_RSCC_VOTE, rd(g + GCC_RSCC_VOTE) | (1u << 20) | (1u << 21));
	if (!gdsc_on(GCC_GDSC_CORE, 1u << 0, "root complex 0's power domain") ||
	    !gdsc_on(GCC_GDSC_PHY, 1u << 2, "the PHY's power domain")) {
		return false;
	}
	if (!rcg_set(GCC_AUX_RCG, 0x001, "aux at 19.2 MHz (crystal, /1)") ||
	    !rcg_set(GCC_RCHNG_RCG, 0x605, "rchng at 100 MHz (GPLL0 even, /3)")) {
		return false;
	}
	wr(tcsr + TCSR_CLKREF_EN, rd(tcsr + TCSR_CLKREF_EN) | 1);
	sleep_us(10);
	wr(g + GCC_BRANCH_VOTE, rd(g + GCC_BRANCH_VOTE) | 0x6f);
	wr(g + GCC_BRANCH_VOTE2, rd(g + GCC_BRANCH_VOTE2) | (1u << 12) | (1u << 19) | (1u << 22));
	for (unsigned i = 0; i < sizeof(branches) / sizeof(branches[0]); i++) {
		if (!poll(g + branches[i], CBCR_CLK_OFF, 0, 10, 1000, "a PCIe clock branch starting")) {
			printf("pcie: power: the branch at GCC %#x stays off\n", branches[i]);
			return false;
		}
	}
	printf("pcie: power: domains on; aux, rchng, reference and branch clocks running\n");
	return true;
}

static void
resets_pulse(void)
{
	vm_offset_t g = w->gcc;

	wr(g + GCC_PCIE0_BCR, rd(g + GCC_PCIE0_BCR) | 1);
	wr(g + GCC_PCIE0_PHY_BCR, rd(g + GCC_PCIE0_PHY_BCR) | 1);
	sleep_us(1000);
	wr(g + GCC_PCIE0_PHY_BCR, rd(g + GCC_PCIE0_PHY_BCR) & ~1u);
	wr(g + GCC_PCIE0_BCR, rd(g + GCC_PCIE0_BCR) & ~1u);
	sleep_us(1000);
}

/* ---- F: PARF (pcie-qcom.c init_2_7_0 and configure_dbi_atu_base) ------------------------------- */

static void
parf_setup(void)
{
	vm_offset_t p = w->parf;

	wr(p + 0x1000, 0x4);                                    /* DEVICE_TYPE: root complex */
	wr(p + 0x040, rd(p + 0x040) & ~1u);                     /* PHY_CTRL: PHY_TEST_PWR_DOWN off */
	wr(p + 0x350, 0x40000000);                              /* DBI_BASE_ADDR_V2 */
	wr(p + 0x354, 0);
	wr(p + 0x634, 0x40001000);                              /* ATU_BASE_ADDR */
	wr(p + 0x638, 0);
	wr(p + 0x358, 0);                                       /* SLV_ADDR_SPACE_SIZE_V2 */
	wr(p + 0x35c, 0x80000000);
	wr(p + 0x000, rd(p + 0x000) & ~(1u << 29));             /* SYS_CTRL: MAC_PHY_POWERDOWN_IN_P2_D_MUX_EN */
	wr(p + 0x174, rd(p + 0x174) | (1u << 4));               /* MHI_CLOCK_RESET_CTRL: BYPASS */
	wr(p + 0x020, rd(p + 0x020) & ~(1u << 5));              /* PM_CTRL: REQ_NOT_ENTR_L1 */
	wr(p + 0x1a8, rd(p + 0x1a8) | (1u << 31));              /* AXI_MSTR_WR_ADDR_HALT_V2: EN */
	wr(p + 0x2c48, rd(p + 0x2c48) & ~1u);                   /* vendor CESTA_CLKREQ_SEL off (qcom,pcie-clkreq-offset) */
}

/* ---- G: the PHY --------------------------------------------------------------------------------- */

#define PHY_SW_RESET            0x400
#define PHY_START_CONTROL       0x444
#define PHY_PCS_STATUS1         0x414
#define PHY_PHYSTATUS           (1u << 6)

static bool
phy_start(void)
{
	vm_offset_t g = w->gcc;

	for (unsigned i = 0; i < sizeof(pcie0_phy_sequence) / sizeof(pcie0_phy_sequence[0]); i++) {
		wr(w->phy + pcie0_phy_sequence[i][0], pcie0_phy_sequence[i][1]);
	}
	wr(g + GCC_PIPE_MUX, rd(g + GCC_PIPE_MUX) & ~3u);
	wr(g + GCC_BRANCH_VOTE, rd(g + GCC_BRANCH_VOTE) | (1u << 4));
	wr(w->phy + PHY_SW_RESET, 0);
	wr(w->phy + PHY_START_CONTROL, 3);
	sleep_us(1200);
	if (!poll(w->phy + PHY_PCS_STATUS1, PHY_PHYSTATUS, 0, 200, 10000, "the PHY's PHYSTATUS clearing")) {
		return false;
	}
	printf("pcie: power: PHY started (%u writes), pipe clock %s\n",
	    (unsigned)(sizeof(pcie0_phy_sequence) / sizeof(pcie0_phy_sequence[0])),
	    (rd(g + 0x6b054) & CBCR_CLK_OFF) ? "still off" : "running");
	return true;
}

/* ---- I: the root port's header (a part of dw_pcie_setup_rc) ------------------------------------ */

#define DBI_RO_WR_EN            0x8bc
#define DBI_LNKCTL2             0xa0                /* the express capability at 0x70 + 0x30 */

static void
root_port_setup(void)
{
	vm_offset_t d = w->dm_core;

	wr(d + DBI_RO_WR_EN, rd(d + DBI_RO_WR_EN) | 1);
	wr(d + 0x10, 0x4);                                      /* BAR0: 64-bit memory */
	wr(d + 0x14, 0);
	wr(d + 0x3c, (rd(d + 0x3c) & 0xffff00ffu) | 0x100);     /* interrupt pin A */
	wr(d + 0x18, (rd(d + 0x18) & 0xff000000u) | 0x00ff0100); /* buses: primary 0, secondary 1, subordinate 0xff */
	wr(d + 0x04, rd(d + 0x04) | 0x107);                     /* IO, memory, bus master, SERR */
	wr(d + 0x10, 0);
	wr(d + 0x08, (rd(d + 0x08) & 0x0000ffffu) | (0x0604u << 16));   /* class: PCI bridge */
	wr(d + DBI_LNKCTL2, (rd(d + DBI_LNKCTL2) & ~0xfu) | 1);  /* target speed Gen1, for this first batch */
	wr(d + DBI_RO_WR_EN, rd(d + DBI_RO_WR_EN) & ~1u);
}

/* ---- the whole --------------------------------------------------------------------------------- */

#define PARF_LTSSM              0x1b0
#define LTSSM_EN                (1u << 8)
#define DBI_LINKCTRLSTATUS      0x80
#define LINK_ACTIVE             (1u << 29)

bool
mhi_pcie_power_up(const struct mhi_pcie_windows *windows)
{
	uint32_t found[sizeof(supplies) / sizeof(supplies[0])], xo, ltssm = 0xff;
	bool up = false;

	w = windows;
	rsc = ml_io_map(RSC_PHYS, RSC_SIZE);
	cmd_db = ml_io_map_wcomb(CMD_DB_PHYS, CMD_DB_SIZE);
	tcsr = ml_io_map(TCSR_CLKREF_PHYS, 0x1000);
	if (rsc == 0 || cmd_db == 0 || tcsr == 0) {
		printf("pcie: power: the RSC, command database or TCSR cannot be mapped\n");
		return false;
	}
	printf("pcie: power: A: GPLL0 %s; RSC version %u.%u\n",
	    (rd(w->gcc + GCC_GPLL0_MODE) & (1u << 31)) ? "locked" : "NOT locked",
	    rd(rsc) >> 16 & 0xff, rd(rsc) >> 8 & 0xff);
	if (!(rd(w->gcc + GCC_GPLL0_MODE) & (1u << 31)) || (rd(rsc) >> 16 & 0xff) != 3 || !supplies_found(found, &xo)) {
		return false;
	}

	mhi_pcie_pause("B: PERST and the card's enable driven low");
	wr(PIN_IO(PIN_PERST), 0);
	wr(PIN_CTL(PIN_PERST), CTL_OUT_LOW_2MA);
	wr(PIN_IO(PIN_WLAN_EN), 0);
	wr(PIN_CTL(PIN_WLAN_EN), CTL_OUT_LOW_8MA);

	mhi_pcie_pause("C: RPMh votes for the PHY's and the card's supplies");
	if (!supplies_on(found, xo)) {
		return false;
	}
	mhi_pcie_pause("D: power domains and clocks");
	if (!clocks_on()) {
		return false;
	}
	mhi_pcie_pause("E, F: resets, then the controller's glue");
	resets_pulse();
	parf_setup();
	mhi_pcie_pause("G: the PHY");
	if (!phy_start()) {
		return false;
	}

	mhi_pcie_pause("H: the card's enable");
	wr(PIN_IO(PIN_WLAN_EN), PIN_IO_OUT_HIGH);
	delay_for_interval(50, NSEC_PER_MSEC);
	wr(PIN_CTL(PIN_CLKREQ), CTL_CLKREQ_FUNCTION);
	wr(PIN_CTL(PIN_WAKE), CTL_IN_PULL_UP);
	printf("pcie: power: the card's enable is high; sw-ctrl pins read %u %u\n",
	    rd(PIN_IO(18)) & 1, rd(PIN_IO(19)) & 1);
	root_port_setup();
	delay_for_interval(50, NSEC_PER_MSEC);              /* 100 ms from the enable to PERST (PCIE_T_PVPERL_MS) */

	wr(PIN_IO(PIN_PERST), PIN_IO_OUT_HIGH);
	sleep_us(1000);
	wr(w->parf + PARF_LTSSM, rd(w->parf + PARF_LTSSM) | LTSSM_EN);
	for (unsigned i = 0; i < 10 && !up; i++) {
		uint32_t state = rd(w->parf + PARF_LTSSM) & 0x3f;

		if (state != ltssm) {
			printf("pcie: power: J: link training, state machine %#x after %u ms\n", state, i * 90);
			ltssm = state;
		}
		up = (rd(w->dm_core + DBI_LINKCTRLSTATUS) & LINK_ACTIVE) != 0;
		if (!up) {
			delay_for_interval(90, NSEC_PER_MSEC);
		}
	}
	printf("pcie: power: the link is %s (state machine %#x, link status %#010x)\n", up ? "up" : "still down",
	    rd(w->parf + PARF_LTSSM) & 0x3f, rd(w->dm_core + DBI_LINKCTRLSTATUS));
	if (up) {
		delay_for_interval(100, NSEC_PER_MSEC);
	}
	return true;
}
