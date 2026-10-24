/*
 * annibale_haptic_data.h -- the constants annibale_haptic.c drives the K90 motor with, every one of
 * them read off this phone's device tree (qcom,hv-haptics@f000) or the vendor's qcom-hv-haptics.c:
 * the SPMI arbiter's windows and its v7 command words, the three haptics peripherals' ppids, the
 * driver's and pattern engine's registers, the motor's own numbers (period, VMAX, brake samples,
 * waveform shapes) and the timing iOS's waveform is judged by.  Pure data, no code; the driver is
 * its only reader -- the state machine, the SPMI layer and the boot-arg levels stay in the .c.
 */
#ifndef _DEV_ANNIBALE_HAPTIC_DATA_H
#define _DEV_ANNIBALE_HAPTIC_DATA_H

/* ---- the SPMI arbiter, version 7 (Linux's spmi-pmic-arb.c: everything >= 0x70000000 is "v7") ------ */

/* the windows of the first controller, the device tree's spmi@c42d000 (reg names as they are there) */
#define ARB_CORE_PHYS           0x0c400000UL
#define ARB_CORE_SIZE           0x003000UL
#define ARB_CNFG_PHYS           0x0c42d000UL
#define ARB_CNFG_SIZE           0x004000UL
#define ARB_CHNLS_PHYS          0x0c500000UL
#define ARB_CHNLS_SIZE          0x00010000UL            /* of its 4M: the channels one uses are far under */
#define ARB_OBSRV_PHYS          0x0c440000UL
#define ARB_OBSRV_SIZE          0x0080000UL

#define ARB_VERSION             0x000
#define ARB_FEATURES            0x004                   /* [10:0] the number of channels */
#define ARB_VERSION_7           0x70000000u

#define ARB_SYSTEM              0                       /* who we are to the arbiter: the application processor */
#define APID_MAX                ((ARB_CORE_SIZE - 0x2000) / 4)  /* v7: the map of what each channel is at core+0x2000 */
#define APID_MAP(n)             (0x2000UL + 4 * (n))    /* core: [19:8] the peripheral (slave and function) */
#define APID_OWNER(n)           (4 * (n))               /* cnfg: [2:0] the system that may write through it */

/* the registers of one channel (the same set in the read window at 0x8000*system + 0x20*apid, and in
 * the write window at 0x1000*apid) */
#define CH_CMD                  0x00
#define CH_STATUS               0x08
#define CH_WDATA0               0x10
#define CH_WDATA1               0x14
#define CH_RDATA0               0x18
#define CH_RDATA1               0x1c
#define STATUS_DONE             0x1
#define STATUS_FAILURE          0x2
#define STATUS_DENIED           0x4
#define STATUS_DROPPED          0x8
#define CMD_OPC(opc)            ((uint32_t)(opc) << 27) /* the opcode, the register [11:4] and the bytes - 1 [2:0] */
#define CMD_REG(reg)            (((uint32_t)(reg) & 0xff) << 4)
#define OPC_EXT_WRITEL          0                       /* eight bytes */
#define OPC_EXT_READL           1                       /* ... read */
#define OPC_EXT_WRITE           2                       /* four bytes */
#define OPC_EXT_READ            13                      /* ... read */
#define TRANSACT_TIMEOUT_US     2000

/*
 * The three peripherals' ppids, slave and function as the arbiter's table holds them: the block
 * sits on the PMIH010X at slave 7 -- the device tree's /soc/qcom,spmi@c42d000/qcom,pmih010x@7/
 * qcom,hv-haptics@f000, line 41612 of the merged decompile -- so 7 << 8 | 0xf0 and kin.
 */
#define HAP_DRIVER              0x7f0                   /* the driver: registers 0xf0xx */
#define HAP_PATTERN             0x7f1                   /* its pattern engine: 0xf1xx */
#define HAP_BOOST               0x7f2                   /* its boost regulator: 0xf2xx (only looked at) */
/* ---- the driver block (HAP): what the device tree of this phone says, distilled ------------------- */

#define HAP_CFG_REVISION        0xf000                  /* two bytes, the minor in the low one (read 2) */
#define HAP_CFG_HPWR_INTF       0xf00b
#define HAP_CFG_FAULT_STATUS    0xf00c
#define HAP_CFG_RT_LRA_IMPED    0xf00e                  /* the motor's impedance, as measured (read 2) */
#define HAP_CFG_INT_RT_STS      0xf010
#define HAP_CFG_EN_CTL          0xf046                  /* bit 7: the module's enable */
#define HAP_EN                  (1u << 7)
#define HAP_CFG_VMAX            0xf048                  /* millivolts / 50 */
#define HAP_CFG_DRV_WF_SEL      0xf049                  /* [1:0] the drive's shape (1 = sine), bit 4: two's complement */
#define HAP_CFG_AUTO_SHUTDOWN   0xf04a
#define HAP_CFG_SPMI_PLAY       0xf04c                  /* [2:0] where the samples come from, bit 3 brake, bit 7 play */
#define HAP_PLAY                (1u << 7)
#define HAP_BRAKE_EN            (1u << 3)
#define HAP_SRC_DIRECT          1                       /* the pattern engine's DIRECT_PLAY amplitude */
#define HAP_CFG_BRAKE_MODE      0xf050                  /* [7:6] brake mode (1 = closed loop), [3:2] sine gain, [1:0] brake shape */
#define HAP_CFG_TLRA_HIGH       0xf05c                  /* the motor's period, two bytes big-endian, steps of 5 us */
#define HAP_CFG_FAULT_CLR       0xf066                  /* bits 2, 1, 0: clear short circuit, auto-res error, hpwr */
#define HAP_FAULT_CLR_ALL       0x07

#define HAP_PTN_REVISION        0xf100
#define HAP_PTN_FIFO_READY      0xf108
#define HAP_PTN_DIRECT_PLAY     0xf126                  /* the amplitude the motor is driven at, 0..255 */
#define HAP_PTN_BRAKE_SAMPLES   0xf170                  /* eight steps, the device tree's ff 3f 1f padded with 0 */

#define LRA_PERIOD_US           5714                    /* the device tree's lra-period-us 0x1652 */
#define VMAX_MV                 1300                    /* its vmax-mv 0x514, of 50 mV steps */

static uint8_t brake_samples[8] = { 0xff, 0x3f, 0x1f, 0x00, 0x00, 0x00, 0x00, 0x00 };

/* ---- hv_haptic=4: the timing iOS's waveform is judged by (the redwood driver's numbers) ------ */

#define ACTIVE_US               30000                   /* a write that was not silence counts for this long */
#define SILENT_BELOW            256                     /* of 32768: samples no larger are silence */
#define TAP_US                  60000                   /* longer than this, and it is a vibration, not a tap */
#define SETTLE_US               8000                    /* how long iOS's waveform is listened to before a tap */
#define FOLLOW_US               20000                   /* how often a vibration's strength follows iOS's */
#define HAPTIC_MAX_MS           1000                    /* no vibration longer, then rest for as long */
#define TOLD_EVENTS             24

#endif /* _DEV_ANNIBALE_HAPTIC_DATA_H */
