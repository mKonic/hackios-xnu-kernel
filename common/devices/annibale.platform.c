/*
 * annibale: paste into xnuloader/platform_private.h next to platform_redwood and add &platform_annibale to platforms[].
 * Every value is explained in docs/PORTING.md ("The loader").
 */
static const struct platform platform_annibale = {
	.name = "annibale",
	.io_base = 0x00400000, .io_size = 0x3FC00000,   /* TODO: one window over the SoC's peripherals, non-zero base */
	.gic_dist = 0x16000000, .gic_redist = 0x16080000,
	.uart = 0,                                      /* phones: no PL011 */
	.log_other_cpu = 1,
	.cores = 8, .core_mpidr = {0, 0x100, 0x200, 0x300, 0x400, 0x500, 0x10000, 0x10100},
	.small_cores = 0x3f,                           /* the six MIDR 0x514f0014 cores: lower capacity cluster */
	.fb_base = 0x0,                                 /* TODO: the framebuffer base the UEFI prints ("display ... at") */
};
