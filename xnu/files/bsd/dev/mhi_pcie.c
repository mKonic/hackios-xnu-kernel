/*
 * The Wi-Fi card of the REDMI K90 (annibale, SM8750 "sun") hangs off a PCIe root complex, which
 * is the structural difference from redwood (the POCO X5 Pro): there bsd/dev/redwood_wifi.c
 * talks to the WCN6750 over SMEM, SMP2P, IPCC and PIL with no PCI anywhere. On this phone the
 * device tree's pcie0 (qcom,pci-msm, the merged decompile's line 34449 onward, the node
 * pcie@1c00000) is a DesignWare root complex whose endpoint is the WCN7850's wifi function: the
 * endpoint's device tree node wlan_kiwi (line 1005, qcom,cnss-kiwi) names supported-ids 0x1107
 * under vendor 0x17cb, and qcom,wlan-rc-num says root complex 0. A first conversation with the
 * card therefore means: read the link's state, read the card's identification out of its config
 * space, and read what its MHI bus says -- all before any firmware is downloaded. This file is
 * that survey, written blind against the vendor's own driver.
 *
 * The vendor's driver for this exact node is drivers/pci/controller/pci-msm.c of the
 * kernel_oneplus_sm8750 branch "sixteen" (the device tree's compatible names it), and this file
 * takes its facts from it: the root complex's own config space (the DBI) is the dm_core window
 * (line 3650: the config base of the root complex's own bus is dm_core); the link is up when
 * dm_core + 0x80 (its PCIE20_CAP 0x70 plus 0x10 LINKCTRLSTATUS, lines 143-145) has bit 29, the
 * data link layer active (line 3459); the raw state machine reads parf + 0x1b0 masked to 0x3f
 * (lines 92, 3504); a DesignWare root complex reaches an endpoint's config space by programming
 * an iATU outbound window to a config type (its cfg_bdf remaps region 0 to bus 1 as type CFG0,
 * lines 3604-3622, register layout lines 173-180); and it refuses endpoint config access with
 * the link down, because such a request would hang the bus (line 3681). This file refuses too.
 *
 * The MHI part follows Linux 6.6: BAR0 of the card maps its register block (drivers/bus/mhi/
 * common.h lines 17-27: MHIREGLEN 0x00, MHIVER 0x08, MHICFG 0x10, CHDBOFF 0x18, ERDBOFF 0x20,
 * BHIOFF 0x28, BHIEOFF 0x2c, MHICTRL 0x38, MHISTATUS 0x48); MHISTATUS bits [15:8] carry the
 * device state and bit 2 its fatal SYS_ERR (common.h 113-114); the states are RESET 0, READY 1,
 * M0 2, M1 3, M2 4, M3 5, M3_FAST 6, BHI 7 and SYS_ERR 0xff (include/linux/mhi.h 150-159).
 *
 * Writing discipline for a blind first run, same as the phone's other _hv files: the survey
 * steps are pure reads. Only two things are ever written, and both are given back before the
 * run ends: iATU region 0, saved whole and restored the way the vendor programs it (line 3593,
 * window off before touching it, enable written last), and the endpoint's BAR registers for the
 * sizing test, written all-ones and restored, every write read back. Nothing happens at all
 * unless a boot-arg asks:
 *
 *   pcie=N   run steps 1 through N, clamped to 6.
 *
 *   step 1  read-only, the clock controller and the pin controller only: the two PCIe power
 *           domains, the root complex's clocks, and the six pins the device tree wires to the
 *           card (wlan-en 16, sw-ctrl 18 and 19, perst 102, clkreq 103, wake 104) as the boot
 *           firmware left them.
 *   step 2  read-only, the root complex itself, 30 s after step 1 so that the host has step 1's
 *           report if these reads stall the SoC, and only with both domains on and the cfg_ahb,
 *           slv_axi, mstr_axi and aux clocks running: the link state machine and the data link
 *           layer's own answer, the PHY's PHYSTATUS bit, 0 once ready (device tree qcom,phy-status-offset
 *           0x414 bit 6, lines 34483-34484), the root complex's own header read from the DBI,
 *           and the whole iATU window as the boot loader left it. (The first run of the survey
 *           with these reads in step 1, kernel 474d6e9, stopped answering USB 8 s into the boot.)
 *   step 3  the endpoint itself: with the link up (the vendor's rule), region 0 is remapped to
 *           CFG and the card's 64-byte header, capability list and BARs are read, with the
 *           sizing test -- 0x17cb:0x1107 is the WCN7850 the device tree expects.
 *   step 4  MHI: if the card got a BAR0 inside the inbound memory window (device tree ranges,
 *           cpu 0x40300000 for 0x3d00000, unmapped), it is mapped and the device's state read.
 *
 *   step 5  read BHI execution environment, status and errors, BHIe transmit status, and the
 *           PARF RID 0x100 to SID table chain. Requires the known WCN7850 and a live link.
 *
 *   step 6  read only SCR0, IDR0 and IDR1 from apps SMMU after checking that PCIe remains powered
 *           and linked. The SMMU window is read once at its global page; SID and context scans are
 *           a later step after this access succeeds on the device.
 *
 * Firmware download and doorbell writes remain separate work. Without
 * pcie_power=1 nothing persists -- everything written is restored -- so there is no quiesce on the
 * way out. pcie_power=1 with pcie=2 or more powers root complex 0 and the card up between steps 1
 * and 2 when step 1 finds them off (mhi_pcie_power.c); that is left on until the reset.
 *
 * The boot-arg is pcie= and not wifi= on purpose: bsd/dev/redwood_wifi.c takes wifi= for the old
 * phone's WCN6750 (there wifi=1 is its production path, its line 4269) and stays as it is -- the
 * names cannot collide, the two drivers cannot be started by one command line by accident, and
 * this phone's command line carries pcie=N. An SoC guard for the old driver is on the backlog.
 */
#include <kern/sched_prim.h>
#include <kern/thread.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <machine/machine_routines.h>
#include <pexpert/pexpert.h>
#include <sys/systm.h>

#include "mhi_pcie_private.h"

extern vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);
extern kern_return_t thread_terminate(thread_t thread);
extern void loader_log_putc(char c);

static bool leaving;

/* every line to the loader's log as well (and to the console when that may be written) */
int
mhi_pcie_say(const char *format, ...)
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

#define printf mhi_pcie_say

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

/* ---- the windows, from the device tree's pcie@1c00000 (lines 34449 ff) and its friends ------ */

#define PARF_PHYS                 0x01c00000UL      /* "parf", 0x3000 */
#define PARF_SIZE                 0x003000UL
#define PHY_PHYS                  0x01c06000UL      /* "phy", 0x2000 */
#define PHY_SIZE                  0x002000UL
#define DM_CORE_PHYS              0x40000000UL      /* "dm_core" 0xf1d, "elbi" 0xa8, "iatu" at +0x1000 for 0x1000 */
#define DM_CORE_SIZE              0x002000UL        /* one mapping covers all three */
#define IATU_OFF                  0x00001000UL
#define CONF_PHYS                 0x40100000UL      /* "conf": the window config accesses go through */
#define CONF_SIZE                 0x0001000UL       /* the vendor's remapped window is 4 KiB (pci-msm.c 3619) */
#define PCIE_SM_PHYS              0x01d07000UL      /* "pcie_sm", 0x7000 */
#define PCIE_SM_SIZE              0x0007000UL
#define GCC_PHYS                  0x00100000UL      /* clock-controller@100000 (device tree line 18520) */
#define GCC_SIZE                  0x006e000UL
/*
 * The pin registers start 0x100000 above the vendor device tree's tlmm reg (0xf000000), as in
 * mainline sm8750.dtsi and redwood_goodix.c's board_setup(). Reading pin 16 at the vendor base
 * plus 0x10000 stopped the SoC (kernel 76c1ad6, pcie_pause=8).
 */
#define TLMM_PHYS                 0x0f100000UL
#define TLMM_SIZE                 0x00102000UL

/* inbound memory, mapped without translation: the device tree's ranges, cpu == pci */
#define PCIE_MEM_BASE             0x40300000UL
#define PCIE_MEM_END              0x44000000UL      /* + 0x3d00000 */

/*
 * The clock controller's power domains for root complex 0: gcc_pcie_0_gdsc at 0x6b004 and
 * gcc_pcie_0_phy_gdsc at 0x6c000, offsets from the platform's clock table (its gdscr fields),
 * read the way Linux' gdsc driver reads a domain: bits [31:30], 0 off and 3 on.
 */
#define GDSC_PCIE0                0x0006b004UL
#define GDSC_PCIE0_PHY            0x0006c000UL
#define GDSC_PWR_ON               (1u << 31)        /* Linux gdsc.c PWR_ON_MASK */

/*
 * The root complex's clock branches (gcc-sm8750.c halt_reg offsets); bit 31 of each CBCR is
 * CLK_OFF (clk-branch.h). The parf, phy and DBI windows answer only with the two domains on and
 * these clocks running: a read of an unclocked block can stall the interconnect and hang the SoC,
 * which USB cannot recover, so step 2 reads none of them otherwise.
 */
#define CBCR_CLK_OFF              (1u << 31)
static const struct { uint32_t offset; const char *name; } pcie0_clocks[] = {
	{ 0x6b040, "cfg_ahb" }, { 0x6b020, "slv_axi" }, { 0x6b030, "mstr_axi" }, { 0x6b044, "aux" },
	{ 0x6b01c, "slv_q2a_axi" }, { 0x6b054, "pipe" },
};
#define PCIE0_CLOCKS_NEEDED       4                 /* cfg_ahb, slv_axi, mstr_axi and aux */

/* the state machine and the vendor's status words (pci-msm.c 92, 143-145, 3459, 3504) */
#define PARF_LTSSM                0x01b0            /* the state is the low 6 bits */
#define PCIE20_DEVCTRLSTATUS      0x0078            /* the express capability at its fixed spot */
#define PCIE20_LINKCTRLSTATUS     0x0080
#define LINK_ACTIVE               (1u << 29)        /* the data link layer: pci-msm.c 3459 */
#define PHY_STATUS                0x0414            /* device tree line 34483... */
#define PHY_NOT_READY             (1u << 6)         /* ... and its bit 6: PHYSTATUS of the QMP PCS, 0 once the PHY is ready */

/* iATU, the vendor's layout (pci-msm.c 173-180); the window holds outbound regions 0 through 7 */
#define IATU_REGION(n)            (0x200UL * (n))
#define IATU_CTRL1(n)             (IATU_REGION(n) + 0x00)   /* [4:0] the type */
#define IATU_CTRL2(n)             (IATU_REGION(n) + 0x04)   /* bit 31 enables the region */
#define IATU_LBAR(n)              (IATU_REGION(n) + 0x08)
#define IATU_UBAR(n)              (IATU_REGION(n) + 0x0c)
#define IATU_LAR(n)               (IATU_REGION(n) + 0x10)   /* the limit, an address not a size */
#define IATU_LTAR(n)              (IATU_REGION(n) + 0x14)
#define IATU_UTAR(n)              (IATU_REGION(n) + 0x18)
#define IATU_ENABLE               (1u << 31)
#define IATU_TYPE_CFG0            0x04              /* type 0 config, the device on the link (line 184) */
#define IATU_TYPE_CFG1            0x05              /* type 1 config, across a bridge (line 185) */
#define BDF(bus, devfn)           ((uint32_t)(bus) << 24 | (uint32_t)(devfn) << 16)     /* pci-msm.c 307 */

/* the endpoint the device tree expects (wlan_kiwi, line 1005: supported-ids <0x1107>) */
#define WCN_VENDOR                0x17cb
#define WCN7850_DEVICE            0x1107

/* the card's register block (Linux 6.6 drivers/bus/mhi/common.h 17-27, 111-114) */
#define MHI_REGLEN                0x00
#define MHI_VER                   0x08              /* [31:16] minor, [15:0] major */
#define MHI_CFG                   0x10
#define MHI_CHDBOFF               0x18
#define MHI_ERDBOFF               0x20
#define MHI_BHIOFF                0x28
#define MHI_BHIEOFF               0x2c
#define MHI_DEBUGOFF              0x30
#define MHI_CTRL                  0x38              /* [15:8] what the host asks for, bit 1 its reset */
#define MHI_STATUS                0x48              /* [15:8] what the device is, bit 2 SYS_ERR */

/* SM8750 apps SMMU, device-tree reg 0x15000000+0x100000. */
#define APPS_SMMU_PHYS             0x15000000UL
#define APPS_SMMU_SIZE             0x00100000UL
#define SMMU_SCR0                  0x000
#define SMMU_IDR0                  0x020
#define SMMU_IDR1                  0x024

static vm_offset_t parf, phy, dm_core, conf, pcie_sm, gcc, tlmm;
static uint32_t ep_vendor, ep_device, ep_bar0, ep_bar1;
static bool ep_alive, link_up;
static uint32_t power;                       /* pcie_power=1: mhi_pcie_power.c, BAR0 assigned */

static const char *
ltssm_name(uint32_t state)
{
	static const char *const names[] = {
		/* 0x00 */ "DETECT_QUIET", "DETECT_ACTIVE", "POLL_ACTIVE", "POLL_COMPLIANCE",
		/* 0x04 */ "POLL_CONFIG", "PRE_DETECT_QUIET", "DETECT_WAIT", "CFG_LINKWD_START",
		/* 0x08 */ "CFG_LINKWD_ACCEPT", "CFG_LANENUM_WAIT", "CFG_LANENUM_ACCEPT",
		/* 0x0c */ "CFG_COMPLETE", "CFG_IDLE", "RCVRY_LOCK", "RCVRY_SPEED",
		/* 0x10 */ "RCVRY_RCVRCFG", "RCVRY_IDLE", "L0", "L0S",
		/* 0x14 */ "L123_SEND_EIDLE", "L1_IDLE", "L2_IDLE", "L2_WAKE",
		/* 0x18 */ "DISABLED_ENTRY", "DISABLED_IDLE", "DISABLED", "LPBK_ENTRY",
		/* 0x1c */ "LPBK_ACTIVE", "LPBK_EXIT", "LPBK_EXIT_TIMEOUT", "HOT_RESET_ENTRY",
		/* 0x20 */ "RCVRY_EQ0", "RCVRY_EQ1", "RCVRY_EQ2", "RCVRY_EQ3"
	};

	if ((state & 0x3f) >= sizeof(names) / sizeof(names[0])) {
		return "beyond the vendor's table";
	}
	return names[state & 0x3f];
}

static const char *
mhi_state_name(uint32_t state)
{
	switch (state) {
	case 0x00:
		return "RESET";
	case 0x01:
		return "READY";
	case 0x02:
		return "M0, booted and running";
	case 0x03:
		return "M1";
	case 0x04:
		return "M2, asleep";
	case 0x05:
		return "M3";
	case 0x06:
		return "M3_FAST";
	case 0x07:
		return "BHI, ready for its host to load firmware";
	case 0xff:
		return "SYS_ERR";
	default:
		return "a state the specification does not name";
	}
}

/* ---- steps 1 and 2: power, pins, link -- every line a read --------------------------------------------------------------------------------- */

static void
survey_windows(void)
{
	parf = ml_io_map(PARF_PHYS, PARF_SIZE);
	phy = ml_io_map(PHY_PHYS, PHY_SIZE);
	dm_core = ml_io_map(DM_CORE_PHYS, DM_CORE_SIZE);
	conf = ml_io_map(CONF_PHYS, CONF_SIZE);
	pcie_sm = ml_io_map(PCIE_SM_PHYS, PCIE_SM_SIZE);
	gcc = ml_io_map(GCC_PHYS, GCC_SIZE);
	tlmm = ml_io_map(TLMM_PHYS, TLMM_SIZE);
	if (parf == 0 || phy == 0 || dm_core == 0 || conf == 0 || pcie_sm == 0 || gcc == 0 || tlmm == 0) {
		printf("pcie: a window cannot be mapped: the survey stops here\n");
	}
}

static const char *
domain_state(uint32_t value)
{
	switch ((value >> 30) & 3) {
	case 0:
		return "off";
	case 3:
		return "on";
	default:
		return "between -- mid-transition or stuck";
	}
}

/* Whether the root complex's registers may be read: both domains on and the needed clocks running. */
static bool
show_power(void)
{
	uint32_t a = rd(gcc + GDSC_PCIE0), b = rd(gcc + GDSC_PCIE0_PHY);
	bool readable = (a & GDSC_PWR_ON) && (b & GDSC_PWR_ON);

	printf("pcie: the clock controller's PCIe power domains: root complex %#010x is %s, the phy %#010x is %s\n",
	    a, domain_state(a), b, domain_state(b));
	for (unsigned i = 0; i < sizeof(pcie0_clocks) / sizeof(pcie0_clocks[0]); i++) {
		uint32_t cbcr = rd(gcc + pcie0_clocks[i].offset);

		printf("pcie: clock %s (CBCR %#lx) %#010x: %s\n", pcie0_clocks[i].name,
		    (unsigned long)(GCC_PHYS + pcie0_clocks[i].offset), cbcr, (cbcr & CBCR_CLK_OFF) ? "off" : "running");
		if (i < PCIE0_CLOCKS_NEEDED && (cbcr & CBCR_CLK_OFF)) {
			readable = false;
		}
	}
	return readable;
}

/*
 * One pin as Android's firmware left it. Layout from the platform's own pin controller driver
 * (pinctrl-sm8750.c): the per-pin blocks are 0x1000 apart; control at +0 holds pull in [1:0] (0 none, 1 down, 2 keeper, 3 up),
 * function in [5:2], drive in [8:6], output enable at bit 9 (its PINGROUP macro, lines 35-43);
 * io at +4 carries the input's value at bit 0 and the output's at bit 1 (in_bit, out_bit,
 * lines 67-68). The function names of the six pins are that driver's lines 1451-1543.
 */
static void
show_pin(const char *what, unsigned pin, unsigned alt1, const char *alt1_name)
{
	uint32_t ctl = rd(tlmm + 0x1000UL * pin), io = rd(tlmm + 0x1000UL * pin + 0x04);
	static const char *const pulls[] = { "no pull", "pull-down", "keeper", "pull-up" };       /* msm_gpio_set_config */
	unsigned mux = (ctl >> 2) & 0xf;

	printf("pcie: GPIO %3u (%s): mux %u (%s), %s, output enable %u, driven %u, input reads %u\n",
	    pin, what, mux, mux == 0 ? "plain gpio" : (mux == alt1 ? alt1_name : "some other function"),
	    pulls[ctl & 3], (ctl >> 9) & 1, (io >> 1) & 1, io & 1);
}

static void
show_link(void)
{
	uint32_t ltssm = rd(parf + PARF_LTSSM) & 0x3f, status = rd(dm_core + PCIE20_LINKCTRLSTATUS);
	uint32_t physt = rd(phy + PHY_STATUS);

	link_up = (status & LINK_ACTIVE) != 0;
	printf("pcie: link: state machine is in %s (%#x); data link layer %s (link status %#010x, device status %#010x)\n",
	    ltssm_name(ltssm), ltssm, link_up ? "is up, the card answered training" : "is down",
	    status, rd(dm_core + PCIE20_DEVCTRLSTATUS));
	printf("pcie: the phy is %s (PCS status %#010x at %#lx)\n",
	    (physt & PHY_NOT_READY) ? "not ready" : "ready", physt, (unsigned long)(PHY_PHYS + PHY_STATUS));
	printf("pcie: the state manager's debug registers: %#010x %#010x %#010x (the boot-args watch the first two)\n",
	    rd(pcie_sm + 0x1040), rd(pcie_sm + 0x1048), rd(pcie_sm + 0x104c));
}

/* the root complex's own header, read straight from the DBI window as the vendor's driver does */
static void
show_root_complex(void)
{
	uint32_t id = rd(dm_core), hdr = rd(dm_core + 0x0c), buses = rd(dm_core + 0x18), cap;
	unsigned hops = 0;

	printf("pcie: the root complex's own identity: %#06x:%#06x, class %#06x revision %#x, header type %#x\n",
	    id & 0xffff, id >> 16, rd(dm_core + 0x08) >> 8, rd(dm_core + 0x08) & 0xff, (hdr >> 16) & 0x7f);
	printf("pcie: its numbering: bus %u, secondary %u, subordinate %u\n",
	    rd(dm_core + 0x18) & 0xff, (buses >> 8) & 0xff, (buses >> 16) & 0xff);
	printf("pcie: its own express capability:\n");
	for (cap = rd(dm_core + 0x34) & 0xfc; cap != 0 && hops++ < 48; cap = rd(dm_core + cap) >> 8 & 0xfc) {
		uint32_t lnk;

		if ((rd(dm_core + cap) & 0xff) != 0x10) {
			continue;
		}
		lnk = rd(dm_core + cap + 0x10);                /* LNKCTL in the low half, LNKSTA in the high */
		printf("pcie:   at %#x: link status %#06x -- speed %u, width x%u, link trained %s\n",
		    cap, lnk >> 16, (lnk >> 16) & 0xf, (lnk >> 20) & 0x3f, (lnk & (1u << 29)) ? "yes" : "no");
		return;
	}
	printf("pcie:   no express capability found\n");
}

static void
show_iatu(void)
{
	vm_offset_t iatu = dm_core + IATU_OFF;

	printf("pcie: the iATU as the boot loader left it (only the regions it touched):\n");
	for (unsigned n = 0; n < 8; n++) {
		uint32_t ctrl1 = rd(iatu + IATU_CTRL1(n)), ctrl2 = rd(iatu + IATU_CTRL2(n));

		if (ctrl1 == 0 && ctrl2 == 0) {
			continue;
		}
		printf("pcie:   region %u: type %#x, %s, cpu %#010x..%#010x -> %#010x%08x\n", n, ctrl1 & 0x1f,
		    (ctrl2 & IATU_ENABLE) ? "enabled" : "disabled", rd(iatu + IATU_LBAR(n)), rd(iatu + IATU_LAR(n)),
		    rd(iatu + IATU_UTAR(n)), rd(iatu + IATU_LTAR(n)));
	}
}

/* ---- step 3: the endpoint's config space, through the vendor's own remap scheme ------------- */

static uint32_t
ep_conf(unsigned where)
{
	return rd(conf + (where & ~3u));
}

static uint32_t region_offset[7] = { 0x00, 0x04, 0x08, 0x0c, 0x10, 0x14, 0x18 };
static const char *const region_name[7] = { "ctrl1", "ctrl2", "lbar", "ubar", "lar", "ltar", "utar" };

static void
region0_read(vm_offset_t iatu, uint32_t save[7])
{
	for (unsigned n = 0; n < 7; n++) {
		save[n] = rd(iatu + region_offset[n]);
	}
}

static void
region0_write(vm_offset_t iatu, const uint32_t want[7])
{
	wr(iatu + IATU_CTRL2(0), 0);                          /* off before anything is touched, pci-msm.c 3593 */
	for (unsigned n = 0; n < 7; n++) {
		if (n == 1) {
			continue;                             /* ctrl2, with its enable bit, goes last */
		}
		wr(iatu + region_offset[n], want[n]);
		if (rd(iatu + region_offset[n]) != want[n]) {
			printf("pcie: region 0's %s did not take: %#010x instead of %#010x\n", region_name[n],
			    rd(iatu + region_offset[n]), want[n]);
		}
	}
	wr(iatu + IATU_CTRL2(0), want[1]);
}

/*
 * The endpoint's BARs, read, sized the ordinary way (all-ones in, size out) and given back
 * exactly as found -- the only device writes this survey makes, and it is undone at once.
 */
static void
show_bars(void)
{
	uint32_t bar1_original, mask, size;
	unsigned io, type;

	ep_bar0 = ep_conf(0x10);
	ep_bar1 = ep_conf(0x14);
	bar1_original = ep_bar1;
	printf("pcie: BAR0 %#010x, BAR1 %#010x, BAR2 %#010x, BAR3 %#010x, BAR4 %#010x, BAR5 %#010x\n",
	    ep_bar0, ep_bar1, ep_conf(0x18), ep_conf(0x1c), ep_conf(0x20), ep_conf(0x24));
	io = ep_bar0 & 1;                             /* bit 0: an IO request rather than memory */
	type = (ep_bar0 >> 1) & 3;                    /* memory: 0 is 32-bit, 2 is 64-bit */
	if (ep_bar0 == 0) {
		printf("pcie: BAR0 reads zero: the boot loader assigned the card no memory, nothing to size\n");
		return;
	}
	wr(conf + 0x10, 0xffffffff);                  /* the ordinary sizing test, given back below */
	if (!io && type == 2) {
		wr(conf + 0x14, 0xffffffff);
	}
	mask = rd(conf + 0x10) & (io ? 0xfffffffcu : 0xfffffff0u);
	size = ~mask + 1u;                            /* 0 means 4 GiB or more */
	printf("pcie: BAR0 sizes to %s%u MiB, type %s%s, %sprefetchable\n", size == 0 ? "at least 4096" : "",
	    size / (1024 * 1024), io ? "IO space" : (type == 2 ? "64-bit memory" : "32-bit memory"),
	    io ? "" : "", (ep_bar0 & (1u << 3)) ? "" : "not ");
	wr(conf + 0x10, ep_bar0);
	if (!io && type == 2) {
		wr(conf + 0x14, bar1_original);
	}
	if (rd(conf + 0x10) != ep_bar0 || (!io && type == 2 && rd(conf + 0x14) != bar1_original)) {
		printf("pcie: the BARs did not come back as found: %#010x %#010x now\n", rd(conf + 0x10),
		    rd(conf + 0x14));
	}
}

static void
show_capabilities(void)
{
	uint32_t head;
	unsigned cap, hops = 0;

	printf("pcie: capabilities:");
	for (cap = ep_conf(0x34) & 0xfc; cap != 0 && hops++ < 48; cap = head >> 8 & 0xfc) {
		head = ep_conf(cap);
		printf(" %u", head & 0xff);
		if ((head & 0xff) == 0x10) {                  /* the card's own express capability */
			uint32_t lnk = ep_conf(cap + 0x10) >> 16;   /* LNKSTA is the capability's +0x12 word */

			printf("(express at %#x: speed %u width x%u trained %s)", cap, lnk & 0xf,
			    (lnk >> 4) & 0x3f, (lnk & (1u << 13)) ? "yes" : "no");
		}
		if ((head >> 8 & 0xfc) == cap) {
			break;                                /* a capability pointing at itself ends the walk */
		}
	}
	printf("\n");
}

/*
 * pcie_power=1 only: the card's 64-bit BAR0 at the first address of the device tree's inbound memory
 * window (ranges: cpu 0x40300000 = pci 0x40300000) aligned to its size -- 0x40400000; a BAR keeps
 * only the bits above its size, so 0x40300000 read back as 0x40200000 (kernel 91e559d) -- memory
 * decoding and bus mastering on, and the root
 * port's memory window over it -- what Linux' enumeration would assign. Kept until the reset.
 */
#define BAR0_SIZE                 0x200000u         /* sized on the card: 2 MiB */

static void
assign_bar0(void)
{
	uint32_t base = (PCIE_MEM_BASE + BAR0_SIZE - 1) & ~(BAR0_SIZE - 1), last = base + BAR0_SIZE - 1;

	wr(conf + 0x10, base | (ep_bar0 & 0xf));
	wr(conf + 0x14, 0);
	wr(conf + 0x04, rd(conf + 0x04) | 0x6);               /* memory space, bus master */
	wr(dm_core + 0x20, (last & 0xfff00000u) | ((base >> 16) & 0xfff0u));   /* the root port's memory window */
	ep_bar0 = ep_conf(0x10);
	ep_bar1 = ep_conf(0x14);
	printf("pcie: BAR0 assigned: %#010x %#010x, command %#06x; the root port forwards %#x..%#x\n", ep_bar0,
	    ep_bar1, ep_conf(0x04) & 0xffff, base, last);
}

static void
probe_endpoint(void)
{
	vm_offset_t iatu = dm_core + IATU_OFF;
	uint32_t save[7], want[7], buses = rd(dm_core + 0x18);
	unsigned secondary = (buses >> 8) & 0xff;

	if (!link_up) {
		printf("pcie: the link is down, so the endpoint is left untouched: the vendor's driver refuses\n"
		    "pcie: its config space the same way (pci-msm.c 3681), a request without a link hanging the bus.\n"
		    "pcie: the boot loader left it down; waking the card (its power pins, perst, the link itself)\n"
		    "pcie: is the next batch's first task, and step 1's pin and domain lines say where to start.\n");
		return;
	}
	region0_read(iatu, save);
	printf("pcie: step 3: region 0 was type %#x %s cpu %#010x..%#010x -> %#010x; remapped to config for the probe\n",
	    save[0] & 0x1f, (save[1] & IATU_ENABLE) ? "enabled" : "disabled", save[2], save[4], save[5]);
	bzero(want, sizeof(want));
	want[0] = secondary <= 1 ? IATU_TYPE_CFG0 : IATU_TYPE_CFG1;
	want[1] = IATU_ENABLE;                                /* the vendor enables what it programs, line 3601 */
	want[2] = CONF_PHYS;
	want[4] = CONF_PHYS + 0xfff;                          /* 4 KiB, pci-msm.c 3619 */
	want[5] = BDF(secondary != 0 ? secondary : 1, 0);     /* bus 1 device 0 function 0, pci-msm.c 3614 */
	region0_write(iatu, want);

	ep_vendor = ep_conf(0x00) & 0xffff;
	ep_device = ep_conf(0x00) >> 16;
	printf("pcie: the endpoint answers %#06x:%#06x, class %#08x revision %#x; the device tree expects %#06x:%#06x\n",
	    ep_vendor, ep_device, ep_conf(0x08) >> 8, ep_conf(0x08) & 0xff, WCN_VENDOR, WCN7850_DEVICE);
	if (ep_vendor == 0xffff || ep_vendor == 0) {
		printf("pcie: nobody answers on the link -- trained but silent, or the wrong bus numbering\n");
	} else {
		ep_alive = ep_vendor == WCN_VENDOR;
		if (ep_alive) {
			printf("pcie: vendor %04x is Qualcomm; device %#x is %s\n", ep_vendor, ep_device,
			    ep_device == WCN7850_DEVICE ? "exactly the WCN7850 of the device tree" :
			    "some other Qualcomm function of this card family");
		}
		show_bars();
		if (power && ep_alive && (ep_bar0 & ~0xfu) == 0 && (ep_bar0 & 0x7) == 4) {
			assign_bar0();
		}
		show_capabilities();
		printf("pcie: command %#010x, interrupt pin %u line %u\n", ep_conf(0x04),
		    (ep_conf(0x3c) >> 16) & 0xff, ep_conf(0x3c) >> 24);
	}
	region0_write(iatu, save);
	printf("pcie: region 0 is given back to the boot loader as it was\n");
}

/* ---- step 4: the card's MHI face ------------------------------------------------------------ */

/* qcom_pcie_config_sid_1_9_0 hashes the big-endian RID with CRC8-MSB, polynomial 0x07. */
static uint8_t
rid_hash(uint16_t rid)
{
	uint8_t crc = 0;

	for (int shift = 8; shift >= 0; shift -= 8) {
		crc ^= (uint8_t)(rid >> shift);
		for (unsigned bit = 0; bit < 8; bit++) {
			crc = (uint8_t)((crc << 1) ^ ((crc & 0x80) ? 0x07 : 0));
		}
	}
	return crc;
}

static void
read_sid_route(void)
{
	const uint16_t rid = 0x100;
	uint32_t cfg, secondary = (rd(dm_core + 0x18) >> 8) & 0xff;
	uint8_t index = rid_hash(rid);
	bool seen[256] = { false };

	if (secondary != 1) {
		printf("pcie: step 5: secondary bus %u; RID 0x100 survey skipped\n", secondary);
		return;
	}
	cfg = rd(parf + 0x2c00);
	printf("pcie: step 5: PARF BDF_TO_SID_CFG %#010x, bypass %u, RID %#x hash %#x\n",
	    cfg, cfg & 1, rid, index);
	if (cfg == 0xffffffffu) {
		printf("pcie: step 5: invalid SID configuration read; table left unread\n");
		return;
	}
	/* Only follow this RID's collision chain. NEXT is an 8-bit index; zero ends the chain. */
	for (unsigned hops = 0; hops < 256; hops++) {
		uint32_t offset = 0x2000 + 4u * index, entry;

		if (seen[index] || offset > PARF_SIZE - sizeof(uint32_t)) {
			printf("pcie: step 5: SID chain loop or invalid offset at index %#x; stopped\n", index);
			return;
		}
		seen[index] = true;
		entry = rd(parf + offset);
		printf("pcie: step 5: SID table[%#x] %#010x, RID %#x relative SID %#x next %#x\n",
		    index, entry, entry >> 16, (entry >> 8) & 0xff, entry & 0xff);
		if (entry == 0xffffffffu) {
			printf("pcie: step 5: invalid SID table read; stopped\n");
			return;
		}
		if ((entry >> 16) == rid) {
			printf("pcie: step 5: RID %#x found, relative SID %#x; DT expects 1 (SID 0x1401), bypass %u\n",
			    rid, (entry >> 8) & 0xff, cfg & 1);
			return;
		}
		if ((entry & 0xff) == 0) {
			printf("pcie: step 5: RID %#x absent from its SID chain\n", rid);
			return;
		}
		index = (uint8_t)entry;
	}
	printf("pcie: step 5: SID chain reached the 256-entry limit\n");
}

/* First apps SMMU access is limited to three global identification/configuration registers. */
static void
read_apps_smmu_header(void)
{
	vm_offset_t smmu;
	uint32_t scr0, idr0, idr1;

	mhi_pcie_pause("step 6: map apps SMMU global registers");
	if (!show_power() || !(rd(dm_core + PCIE20_LINKCTRLSTATUS) & LINK_ACTIVE) ||
	    ep_vendor != WCN_VENDOR || ep_device != WCN7850_DEVICE) {
		printf("pcie: step 6: PCIe power, link or endpoint check failed; apps SMMU left unread\n");
		return;
	}
	printf("pcie: step 6: map apps SMMU global page at %#lx (window %#lx); next read SCR0/IDR0/IDR1\n",
	    (unsigned long)APPS_SMMU_PHYS, (unsigned long)APPS_SMMU_SIZE);
	mhi_pcie_pause("step 6: read apps SMMU SCR0, IDR0 and IDR1");
	smmu = ml_io_map(APPS_SMMU_PHYS, 0x1000);
	if (smmu == 0) {
		printf("pcie: step 6: apps SMMU global page mapping failed\n");
		return;
	}
	printf("pcie: step 6: apps SMMU global page mapped; reading SCR0 at offset %#x\n", SMMU_SCR0);
	scr0 = rd(smmu + SMMU_SCR0);
	idr0 = rd(smmu + SMMU_IDR0);
	idr1 = rd(smmu + SMMU_IDR1);
	if (scr0 == UINT32_MAX || idr0 == UINT32_MAX || idr1 == UINT32_MAX) {
		printf("pcie: step 6: invalid apps SMMU global read: SCR0 %#010x IDR0 %#010x IDR1 %#010x\n",
		    scr0, idr0, idr1);
		return;
	}
	printf("pcie: step 6: apps SMMU SCR0 %#010x IDR0 %#010x IDR1 %#010x; no SMR/S2CR or context registers read\n",
	    scr0, idr0, idr1);
}

/* BHI/BHIe offsets are device supplied. Keep every read in the already mapped first 4 KiB. */
static bool
boot_registers_fit(uint32_t offset, uint32_t last)
{
	return offset >= 0x100 && (offset & 3) == 0 && offset <= 0x1000 - last - sizeof(uint32_t);
}

static void
read_mhi_boot(vm_offset_t mhi, uint32_t where)
{
	uint32_t bhi, bhie;

	mhi_pcie_pause("step 5: BHI, BHIe and RID-to-SID registers");
	if (!show_power()) {
		printf("pcie: step 5: power or clocks unavailable; stopped\n");
		return;
	}
	if (!(rd(dm_core + PCIE20_LINKCTRLSTATUS) & LINK_ACTIVE) ||
	    ep_vendor != WCN_VENDOR || ep_device != WCN7850_DEVICE ||
	    (where & (BAR0_SIZE - 1)) != 0 || where > PCIE_MEM_END - BAR0_SIZE) {
		printf("pcie: step 5: link, endpoint or BAR0 range check failed; stopped\n");
		return;
	}
	printf("pcie: step 5: read BHI and BHIe status\n");
	bhi = rd(mhi + MHI_BHIOFF);
	bhie = rd(mhi + MHI_BHIEOFF);
	if (boot_registers_fit(bhi, 0x3c)) {
		uint32_t ee = rd(mhi + bhi + 0x28), status = rd(mhi + bhi + 0x2c);
		static const char *const names[] = { "PBL", "SBL", "AMSS", "RDDM", "WFW", "PTHRU", "EDL", "FP" };

		printf("pcie: step 5: BHI offset %#x EXECENV %#x (%s) STATUS %#010x (transfer %u)\n",
		    bhi, ee, ee < sizeof(names) / sizeof(names[0]) ? names[ee] : "unknown", status, status >> 30);
		printf("pcie: step 5: BHI ERRCODE %#010x ERRDBG1 %#010x ERRDBG2 %#010x ERRDBG3 %#010x\n",
		    rd(mhi + bhi + 0x30), rd(mhi + bhi + 0x34), rd(mhi + bhi + 0x38), rd(mhi + bhi + 0x3c));
	} else {
		printf("pcie: step 5: BHI offset %#x outside aligned first-page range; skipped\n", bhi);
	}
	if (boot_registers_fit(bhie, 0x44)) {
		uint32_t status = rd(mhi + bhie + 0x44);

		printf("pcie: step 5: BHIe offset %#x TXVECSTATUS %#010x (transfer %u sequence %#x)\n",
		    bhie, status, status >> 30, status & 0x3fffffffu);
	} else {
		printf("pcie: step 5: BHIe offset %#x outside aligned first-page range; skipped\n", bhie);
	}
	printf("pcie: step 5: read PARF RID-to-SID configuration\n");
	read_sid_route();
}

static void
read_mhi(bool boot_status)
{
	vm_offset_t mhi;
	uint32_t ctrl, status, state, where = ep_bar0 & ~0xfu;

	if (!ep_alive) {
		return;
	}
	if (where == 0 || (ep_bar0 & 1) != 0) {
		printf("pcie: no memory window at BAR0 (%#010x as found): the boot loader assigned nothing to\n"
		    "pcie: the card (or gave it IO space), so its register block cannot be read yet. Assigning\n"
		    "pcie: BAR0 from the inbound window comes before any MHI conversation.\n", ep_bar0);
		return;
	}
	if ((ep_bar0 & 0x6) == 4 && ep_bar1 != 0) {
		printf("pcie: BAR0 is 64-bit memory above 4 GiB (upper half %#x): outside what ml_io_map is\n"
		    "pcie: asked to reach in this batch\n", ep_bar1);
		return;
	}
	if (where < PCIE_MEM_BASE || where >= PCIE_MEM_END) {
		printf("pcie: BAR0 (%#x) is outside the device tree's unmapped window (%#lx..%#lx); this batch\n"
		    "pcie: reads only what the firmware's ranges map one to one, so the MHI face waits\n", where,
		    (unsigned long)PCIE_MEM_BASE, (unsigned long)PCIE_MEM_END);
		return;
	}
	if (power) {
		vm_offset_t iatu = dm_core + IATU_OFF;

		/* outbound region 1, memory: cpu == pci over the BAR (dw_pcie_prog_outbound_atu's layout) */
		wr(iatu + IATU_CTRL2(1), 0);
		wr(iatu + IATU_CTRL1(1), 0);
		wr(iatu + IATU_LBAR(1), where);
		wr(iatu + IATU_UBAR(1), 0);
		wr(iatu + IATU_LAR(1), where + BAR0_SIZE - 1);
		wr(iatu + IATU_LTAR(1), where);
		wr(iatu + IATU_UTAR(1), 0);
		wr(iatu + IATU_CTRL2(1), IATU_ENABLE);
		printf("pcie: outbound region 1 maps %#x..%#x to the same PCI addresses (ctrl2 %#010x)\n", where,
		    where + BAR0_SIZE - 1, rd(iatu + IATU_CTRL2(1)));
	}
	mhi = ml_io_map(where & ~0xfffUL, 0x1000 + (where & 0xfff));
	if (mhi == 0) {
		printf("pcie: the card's BAR0 at %#x cannot be mapped\n", where);
		return;
	}
	mhi += where & 0xfff;
	printf("pcie: step 4: the card's register block at %#x:\n", where);
	printf("pcie:   register length %#010x, version %#010x (major %u minor %u), config %#010x\n",
	    rd(mhi + MHI_REGLEN), rd(mhi + MHI_VER), rd(mhi + MHI_VER) & 0xffff, rd(mhi + MHI_VER) >> 16,
	    rd(mhi + MHI_CFG));
	printf("pcie:   channel doorbell %#010x, event doorbell %#010x, BHI %#010x, BHIe %#010x, debug %#010x\n",
	    rd(mhi + MHI_CHDBOFF), rd(mhi + MHI_ERDBOFF), rd(mhi + MHI_BHIOFF), rd(mhi + MHI_BHIEOFF),
	    rd(mhi + MHI_DEBUGOFF));
	ctrl = rd(mhi + MHI_CTRL);
	status = rd(mhi + MHI_STATUS);
	state = (status >> 8) & 0xff;
	printf("pcie:   host control %#010x (asks for %#x, reset %u), device status %#010x\n", ctrl,
	    (ctrl >> 8) & 0xff, (ctrl >> 1) & 1, status);
	printf("pcie: the card's MHI is in state %s%s: %s\n", mhi_state_name(state),
	    (status & (1u << 2)) ? " and cries SYS_ERR" : "",
	    state == 0x07 ? "exactly the state a host takes over from, firmware load next -- which this batch does not do" :
	    state == 0x02 ? "the firmware is already running, loaded by the boot loader or by Android's last breath" :
	    state == 0x00 ? "the device is held in reset: nobody powered its bus up" :
	    (status & (1u << 2)) || state == 0xff ? "the card hit an internal error before we got here" :
	    "readable, which is all this batch came for");
	if (boot_status) {
		read_mhi_boot(mhi, where);
	}
}

/* ---- the run -------------------------------------------------------------------------------- */

/*
 * pcie_pause=N: N seconds before each part of step 1 (mapping, the clock controller, each pin), each
 * announced first. With pcie=1 and pcie=2 the kernel stopped answering USB within 8 s of the boot
 * (kernels 474d6e9 and 32bbef4) and the log the host had held no line of this file; the pauses let
 * the host's log name the part that stops it. With them (7e54fac) the mapping and the clock
 * controller's reads passed and the stop came with the pin reads.
 */
static uint32_t pause_s;

/* the wlan_kiwi node's pins (device tree line 1005: wlan-en 16, sw-ctrl 18/19) and pcie0's (line 34490) */
static const struct { const char *what; unsigned pin; const char *alt1; } card_pins[] = {
	{ "the card's power enable", 16, "qup2_se4" },
	{ "a control pin", 18, "wcn_sw_ctrl" },
	{ "a control pin", 19, "wcn_sw" },
	{ "PERST, the card's reset", 102, "phase_flag" },
	{ "CLKREQ -- mux 1 means the phy owns it", 103, "pcie0_clk_req_n" },
	{ "WAKE", 104, "pll_bist_sync" },
};

void
mhi_pcie_pause(const char *what)
{
	if (pause_s == 0) {
		return;
	}
	printf("pcie: next, in %u s: %s\n", pause_s, what);
	assert_wait_timeout((event_t)&pause_s, THREAD_UNINT, pause_s * 1000, NSEC_PER_MSEC);
	thread_block(THREAD_CONTINUE_NULL);
}

static void
pcie_thread(void *parameter, __unused wait_result_t result)
{
	uint32_t steps = (uint32_t)(uintptr_t)parameter;
	bool readable;

	mhi_pcie_pause("map the windows");
	printf("pcie: step 1: map root complex 0 (%#lx, %#lx, %#lx) and read what the boot loader left\n",
	    (unsigned long)PARF_PHYS, (unsigned long)DM_CORE_PHYS, (unsigned long)CONF_PHYS);
	survey_windows();
	if (parf == 0 || phy == 0 || dm_core == 0 || conf == 0 || pcie_sm == 0 || gcc == 0 || tlmm == 0) {
		goto out;
	}
	mhi_pcie_pause("read the clock controller");
	readable = show_power();
	for (unsigned i = 0; i < sizeof(card_pins) / sizeof(card_pins[0]); i++) {
		char what[48];

		snprintf(what, sizeof(what), "read GPIO %u in the pin controller", card_pins[i].pin);
		mhi_pcie_pause(what);
		show_pin(card_pins[i].what, card_pins[i].pin, 1, card_pins[i].alt1);
	}
	if (steps < 2) {
		goto out;
	}
	if (!readable && power) {
		struct mhi_pcie_windows windows = { parf, phy, dm_core, gcc, tlmm };

		printf("pcie: root complex 0 is off; pcie_power=1: powering it and the card up (mhi_pcie_power.c)\n");
		if (!mhi_pcie_power_up(&windows)) {
			printf("pcie: the power-up stopped; nothing more is read\n");
			goto out;
		}
		readable = show_power();
	}
	if (!readable) {
		printf("pcie: root complex 0 is powered down or unclocked, so its registers are left unread;\n"
		    "pcie: pcie_power=1 powers it up (mhi_pcie_power.c)\n");
		goto out;
	}
	printf("pcie: step 2 in 30 s: the root complex's own registers\n");
	assert_wait_timeout((event_t)&steps, THREAD_UNINT, 30000, NSEC_PER_MSEC);
	thread_block(THREAD_CONTINUE_NULL);
	show_link();
	show_root_complex();
	show_iatu();
	if (steps < 3) {
		goto out;
	}
	printf("pcie: step 3: look at the endpoint through the vendor's config window\n");
	probe_endpoint();
	if (steps < 4 || !link_up) {
		goto out;
	}
	printf("pcie: step 4: read the MHI face of the card, if it has one yet\n");
	read_mhi(steps >= 5);
	if (steps >= 6) {
		read_apps_smmu_header();
	}

out:
	printf("pcie: the survey is over: %s%s; %s\n",
	    link_up ? "the link is up" : "the link is down", ep_alive ? " and the card identified itself" : "",
	    power ? "the power-up and BAR0 stay until the reset" : "everything written was given back");
	thread_terminate(current_thread());
}

void
mhi_pcie_init(void)
{
	uint32_t steps;
	thread_t thread;

	/* pcie=N runs steps 1 through N; wifi= stays the old phone's driver, see the header */
	if (!PE_parse_boot_argn("pcie", &steps, sizeof(steps)) || steps == 0) {
		return;
	}
	PE_parse_boot_argn("pcie_pause", &pause_s, sizeof(pause_s));
	PE_parse_boot_argn("pcie_power", &power, sizeof(power));
	if (steps > 6) {
		steps = 6;
	}
	if (kernel_thread_start(pcie_thread, (void *)(uintptr_t)steps, &thread) == KERN_SUCCESS) {
		thread_deallocate(thread);
	}
}
