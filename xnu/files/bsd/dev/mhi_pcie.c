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
 *   pcie=N   run steps 1 through N, clamped to 3.
 *
 *   step 1  read-only survey: the two PCIe power domains in the clock controller, the six pins
 *           the device tree wires to the card (wlan-en 16, sw-ctrl 18 and 19, perst 102,
 *           clkreq 103, wake 104) as Android's firmware left them, the link state machine and
 *           the data link layer's own answer, the phy's power-present bit (device tree
 *           qcom,phy-status-offset 0x414 bit 6, lines 34483-34484), the root complex's own
 *           header read from the DBI, and the whole iATU window as the boot loader left it.
 *   step 2  the endpoint itself: with the link up (the vendor's rule), region 0 is remapped to
 *           CFG and the card's 64-byte header, capability list and BARs are read, with the
 *           sizing test -- 0x17cb:0x1107 is the WCN7850 the device tree expects.
 *   step 3  MHI: if the card got a BAR0 inside the inbound memory window (device tree ranges,
 *           cpu 0x40300000 for 0x3d00000, unmapped), it is mapped and the device's state read.
 *
 * No firmware download, no BHI, no doorbells: those are the batches after this one. Nothing
 * persists -- everything written is restored -- so there is no quiesce on the way out.
 *
 * The boot-arg is pcie= and not wifi= on purpose: bsd/dev/redwood_wifi.c takes wifi= for the old
 * phone's WCN6750 (there wifi=1 is its production path, its line 4269) and stays as it is -- the
 * names cannot collide, the two drivers cannot be started by one command line by accident, and
 * this phone's command line carries pcie=3. An SoC guard for the old driver is on the backlog.
 */
#include <kern/thread.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <machine/machine_routines.h>
#include <pexpert/pexpert.h>
#include <sys/systm.h>

extern vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);
extern kern_return_t thread_terminate(thread_t thread);
extern void loader_log_putc(char c);

static bool leaving;

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
#define TLMM_PHYS                 0x0f000000UL      /* the SM8750's 216 pins, as redwood_goodix.c line 1513 */
#define TLMM_SIZE                 0x00202000UL

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

/* the state machine and the vendor's status words (pci-msm.c 92, 143-145, 3459, 3504) */
#define PARF_LTSSM                0x01b0            /* the state is the low 6 bits */
#define PCIE20_DEVCTRLSTATUS      0x0078            /* the express capability at its fixed spot */
#define PCIE20_LINKCTRLSTATUS     0x0080
#define LINK_ACTIVE               (1u << 29)        /* the data link layer: pci-msm.c 3459 */
#define PHY_STATUS                0x0414            /* device tree line 34483... */
#define PHY_POWER_PRESENT         (1u << 6)         /* ... and its bit 6 */

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

static vm_offset_t parf, phy, dm_core, conf, pcie_sm, gcc, tlmm;
static uint32_t ep_vendor, ep_device, ep_bar0, ep_bar1;
static bool ep_alive, link_up;

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

/* ---- step 1: power, pins, link -- every line a read ------------------------------------------------------------------------------------ */

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

static void
show_power(void)
{
	uint32_t a = rd(gcc + GDSC_PCIE0), b = rd(gcc + GDSC_PCIE0_PHY);

	printf("pcie: the clock controller's PCIe power domains: root complex %#010x is %s, the phy %#010x is %s\n",
	    a, domain_state(a), b, domain_state(b));
}

/*
 * One pin as Android's firmware left it. Layout from the platform's own pin controller driver
 * (pinctrl-sm8750.c): the per-pin blocks are 0x1000 apart; control at +0 holds pull in [2:0],
 * function in [5:2], drive in [8:6], output enable at bit 9 (its PINGROUP macro, lines 35-43);
 * io at +4 carries the input's value at bit 0 and the output's at bit 1 (in_bit, out_bit,
 * lines 67-68). The function names of the six pins are that driver's lines 1451-1543.
 */
static void
show_pin(const char *what, unsigned pin, unsigned alt1, const char *alt1_name)
{
	uint32_t ctl = rd(tlmm + 0x1000UL * pin), io = rd(tlmm + 0x1000UL * pin + 0x04);
	static const char *const pulls[] = { "no pull", "pull-up", "pull-down", "keeper" };
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
	printf("pcie: the phy says power present is %s (%#010x at %#lx)\n",
	    (physt & PHY_POWER_PRESENT) ? "there" : "missing", physt, (unsigned long)(PHY_PHYS + PHY_STATUS));
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
	    id & 0xffff, id >> 16, rd(dm_core + 0x08) & 0xffffff, rd(dm_core + 0x08) >> 24, (hdr >> 16) & 0x7f);
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
		    cap, lnk >> 16, lnk & 0xf, (lnk >> 20) & 0x3f, (lnk & (1u << 29)) ? "yes" : "no");
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

/* ---- step 2: the endpoint's config space, through the vendor's own remap scheme ------------- */

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
	printf("pcie: step 2: region 0 was type %#x %s cpu %#010x..%#010x -> %#010x; remapped to config for the probe\n",
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
	printf("pcie: the endpoint answers %#06x:%#06x, class %#06x revision %#x; the device tree expects %#06x:%#06x\n",
	    ep_vendor, ep_device, ep_conf(0x08) & 0xffffff, ep_conf(0x08) >> 24, WCN_VENDOR, WCN7850_DEVICE);
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
		show_capabilities();
		printf("pcie: command %#010x, interrupt pin %u line %u\n", ep_conf(0x04),
		    (ep_conf(0x3c) >> 16) & 0xff, ep_conf(0x3c) >> 24);
	}
	region0_write(iatu, save);
	printf("pcie: region 0 is given back to the boot loader as it was\n");
}

/* ---- step 3: the card's MHI face ------------------------------------------------------------ */

static void
read_mhi(void)
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
	mhi = ml_io_map(where & ~0xfffUL, 0x1000 + (where & 0xfff));
	if (mhi == 0) {
		printf("pcie: the card's BAR0 at %#x cannot be mapped\n", where);
		return;
	}
	mhi += where & 0xfff;
	printf("pcie: step 3: the card's register block at %#x:\n", where);
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
}

/* ---- the run -------------------------------------------------------------------------------- */

static void
pcie_thread(void *parameter, __unused wait_result_t result)
{
	uint32_t steps = (uint32_t)(uintptr_t)parameter;

	printf("pcie: step 1: map root complex 0 (%#lx, %#lx, %#lx) and read what the boot loader left\n",
	    (unsigned long)PARF_PHYS, (unsigned long)DM_CORE_PHYS, (unsigned long)CONF_PHYS);
	survey_windows();
	if (parf == 0 || phy == 0 || dm_core == 0 || conf == 0 || pcie_sm == 0 || gcc == 0 || tlmm == 0) {
		goto out;
	}
	show_power();
	show_pin("the card's power enable", 16, 1, "qup2_se4");           /* the wlan_kiwi node's pins, */
	show_pin("a control pin", 18, 1, "wcn_sw_ctrl");                  /* device tree line 1005:    */
	show_pin("a control pin", 19, 1, "wcn_sw");                       /* wlan-en 16, sw-ctrl 18/19 */
	show_pin("PERST, the card's reset", 102, 1, "phase_flag");        /* perst-gpio <0x27 0x66>, device tree */
	show_pin("CLKREQ -- mux 1 means the phy owns it", 103, 1, "pcie0_clk_req_n");   /* line 34490 */
	show_pin("WAKE", 104, 1, "pll_bist_sync");
	show_link();
	show_root_complex();
	show_iatu();
	if (steps < 2) {
		goto out;
	}
	printf("pcie: step 2: look at the endpoint through the vendor's config window\n");
	probe_endpoint();
	if (steps < 3 || !link_up) {
		goto out;
	}
	printf("pcie: step 3: read the MHI face of the card, if it has one yet\n");
	read_mhi();

out:
	printf("pcie: the survey is over: %s%s; everything written was given back\n",
	    link_up ? "the link is up" : "the link is down", ep_alive ? " and the card identified itself" : "");
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
	if (steps > 3) {
		steps = 3;
	}
	if (kernel_thread_start(pcie_thread, (void *)(uintptr_t)steps, &thread) == KERN_SUCCESS) {
		thread_deallocate(thread);
	}
}
