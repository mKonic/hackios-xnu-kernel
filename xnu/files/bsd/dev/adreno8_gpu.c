/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Copyright (c) 2021, The Linux Foundation. All rights reserved.
 * Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
 *
 * SM8750 gen8_0_0 cold-boot command probe, based on Qualcomm's gen8_start,
 * gen8_patch_pwrup_reglist and gen8_hwsched_cp_init. Source revision and
 * register-table copyright notices are in adreno8_gpu_private.h.
 * Every allocation and the GPU OOB request remain owned until cold reset.
 */
#include "adreno8_private.h"
#include "adreno8_memory_private.h"
#include "adreno8_hwsched_private.h"
#include "adreno8_gpu_private.h"

#define printf adreno8_say
#define A8_COUNT(a) (sizeof(a) / sizeof((a)[0]))

enum gpu_state { GPU_COLD, GPU_PREPARED, GPU_CP_READY, GPU_UNSECURE, GPU_TESTED, GPU_FAILED };
static enum gpu_state gpu_state;
static struct adreno8_memory *sqe, *aqe, *powerup, *scratch;
static bool oob_requested, oob_acked;

static bool
gpu_fail(const char *what)
{
	gpu_state = GPU_FAILED;
	printf("adreno8: GPU %s failed; OOB requested=%u acknowledged=%u; resources retained until cold reset\n",
	    what, oob_requested, oob_acked);
	return false;
}

static uint32_t
gpu_read(uint32_t reg)
{
	return adreno8_rd(adreno8_gpu.base + 4u * reg);
}

static void
gpu_write(uint32_t reg, uint32_t value)
{
	adreno8_wr(adreno8_gpu.base + 4u * reg, value);
}

static bool
gpu_aperture(uint32_t pipe)
{
	gpu_write(GEN8_CP_APERTURE_CNTL_HOST, pipe << 12);
	adreno8_settle();
	if (gpu_read(GEN8_CP_APERTURE_CNTL_HOST) != pipe << 12)
		return gpu_fail("aperture readback");
	return true;
}

static bool
gpu_pipe_write(uint32_t pipe, uint32_t reg, uint32_t value)
{
	if (!gpu_aperture(pipe))
		return false;
	gpu_write(reg, value);
	return true;
}

static bool
gpu_accessible(void)
{
	if (gpu_state == GPU_FAILED || !oob_acked || !adreno8_hwsched_ready() ||
	    !adreno8_gpu_memory_ready() || adreno8_gpu_faulted())
		return false;
	return GMU_PWR_GX_UP(adreno8_rd(adreno8_gmu.base + GMU_PWR_CLK_STATUS));
}

static bool
gpu_status_ok(void)
{
	uint32_t interrupt, cp;
	if (!gpu_accessible())
		return gpu_fail("power, HFI or translation check");
	interrupt = gpu_read(GEN8_RBBM_INT_0_STATUS);
	cp = gpu_read(GEN8_CP_INTERRUPT_STATUS_GLOBAL);
	if ((interrupt & A8_GPU_ERROR_MASK) || (cp & A8_CP_GLOBAL_MASK)) {
		printf("adreno8: GPU fault: RBBM_INT=%#x CP_GLOBAL=%#x\n", interrupt, cp);
		return gpu_fail("CP status");
	}
	return true;
}

static bool
gpu_wait_idle(const char *what)
{
	for (unsigned i = 0; i < 2000; i++) {
		if (!gpu_status_ok())
			return false;
		if (!(adreno8_rd(adreno8_gmu.base + GMU(GEN8_GMUAO_GPU_CX_BUSY_STATUS)) & (1u << 23)))
			return gpu_status_ok();
		adreno8_sleep_us(100);
	}
	return gpu_fail(what);
}

static bool
gpu_raw(const uint32_t *words, uint32_t count, const char *what)
{
	if (!gpu_status_ok())
		return false;
	if (!count || !adreno8_hwsched_raw(words, count, what))
		return gpu_fail(what);
	return gpu_wait_idle(what);
}

static bool
gpu_load_ucode(const char *name, struct adreno8_memory **out)
{
	uint64_t size = 0;
	int disk = 0;
	const uint8_t *image = redwood_image_file("fw", name, &size, &disk);
	if (image == NULL || size < 8 || size > 0x1000000 || (size & 3))
		return gpu_fail("microcode file bounds");
	*out = adreno8_gpu_alloc(name, (uint32_t)size - 4, 4096, ADRENO8_MEM_READ | ADRENO8_MEM_EXEC);
	if (*out == NULL)
		return gpu_fail("microcode allocation");
	memcpy((void *)(*out)->memory, image + 4, (size_t)size - 4);
	if (!adreno8_memory_publish(*out, 0, (uint32_t)size - 4))
		return gpu_fail("microcode publication");
	printf("adreno8: GPU %s payload=%llu IOVA=%#x\n", name, size - 4, (*out)->gpu_address);
	return true;
}

bool
adreno8_gpu_prepare(void)
{
	if (gpu_state != GPU_COLD)
		return gpu_fail("duplicate preparation");
	if (!gpu_load_ucode("gen80000_sqe.fw", &sqe) || !gpu_load_ucode("gen80000_aqe.fw", &aqe))
		return false;
	powerup = adreno8_gpu_alloc("powerup list", 4096, 4096,
	    ADRENO8_MEM_READ | ADRENO8_MEM_WRITE | ADRENO8_MEM_PRIV);
	scratch = adreno8_gpu_alloc("CP scratch", 4096, 4096,
	    ADRENO8_MEM_READ | ADRENO8_MEM_WRITE | ADRENO8_MEM_PRIV);
	if (!powerup || !scratch)
		return gpu_fail("GPU buffer allocation");
	memset((void *)scratch->memory, 0, scratch->size);
	if (!adreno8_memory_publish(scratch, 0, scratch->size))
		return gpu_fail("scratch publication");
	gpu_state = GPU_PREPARED;
	return true;
}

static bool
gpu_oob_get(void)
{
	if (!adreno8_hwsched_ready() || !adreno8_gpu_memory_ready() || adreno8_gpu_faulted())
		return gpu_fail("OOB prerequisites");
	/* gen8_gmu_oob_set(oob_gpu): request bit 30, response bit 31. */
	adreno8_reset_required = true;
	oob_requested = true;
	adreno8_wr(adreno8_gmu.base + GMU_HOST2GMU_SET, 1u << 30);
	adreno8_settle();
	for (unsigned i = 0; i < 1000; i++) {
		if (adreno8_rd(adreno8_gmu.base + GMU_GMU2HOST_INFO) & (1u << 31)) {
			oob_acked = true;
			break;
		}
		adreno8_sleep_us(100);
	}
	adreno8_wr(adreno8_gmu.base + GMU_GMU2HOST_CLR, 1u << 31);
	adreno8_settle();
	if (!oob_acked || !gpu_accessible())
		return gpu_fail("OOB acknowledgement or GX power");
	printf("adreno8: GPU OOB acknowledged and held until cold reset\n");
	return true;
}

static bool
gpu_noncontext(void)
{
	for (unsigned pipe = 0; pipe <= 5; pipe++) {
		if (!gpu_aperture(pipe))
			return false;
		for (unsigned i = 0; i < A8_COUNT(a8_noncontext); i++)
			if (a8_noncontext[i].pipes & (1u << pipe))
				gpu_write(a8_noncontext[i].reg, a8_noncontext[i].value);
	}
	return gpu_aperture(0);
}

static bool
gpu_protect(void)
{
	uint32_t last = 0;
	for (unsigned pipe = 1; pipe <= 2; pipe++)
		if (!gpu_pipe_write(pipe, GEN8_CP_PROTECT_CNTL_PIPE, 0xffff000b))
			return false;
	if (!gpu_aperture(0))
		return false;
	for (unsigned i = 0; i < A8_COUNT(a8_protected); i++) {
		const struct a8_protect_span *p = &a8_protected[i];
		last = p->start | ((p->end - p->start) << 18) | (p->noaccess << 31);
		gpu_write(p->reg, last);
	}
	for (unsigned pipe = 1; pipe <= 2; pipe++)
		if (!gpu_pipe_write(pipe, GEN8_CP_PROTECT_REG_PIPE + 15, last))
			return false;
	return gpu_aperture(0);
}

static bool
gpu_hwcg(void)
{
	gpu_write(GEN8_RBBM_CGC_0_PC, 0x702);
	for (unsigned i = 0; i < A8_COUNT(a8_ao_hwcg); i++)
		adreno8_wr(adreno8_gmu.base + GMU(a8_ao_hwcg[i].reg), a8_ao_hwcg[i].value);
	gpu_write(GEN8_RBBM_CLOCK_CNTL_GLOBAL, 1);
	gpu_write(GEN8_RBBM_CGC_GLOBAL_LOAD_CMD, 1);
	gpu_write(GEN8_RBBM_CGC_P2S_TRIG_CMD, 1);
	adreno8_settle();
	for (unsigned i = 0; i < 3; i++) {
		adreno8_sleep_us(1);
		if (gpu_read(GEN8_RBBM_CGC_P2S_STATUS) & 1) {
			gpu_write(GEN8_RBBM_CLOCK_CNTL_GLOBAL, 0);
			return true;
		}
	}
	return gpu_fail("clock-gating TXDONE");
}

static bool
gpu_powerup_list(void)
{
	uint32_t *words = (uint32_t *)powerup->memory;
	uint32_t at = 4, dynamic = 0;
	uint32_t dynamic_count = 0;
	for (unsigned pipe = 1; pipe <= 7; pipe++)
		for (unsigned i = 0; i < A8_COUNT(a8_powerup_ext); i++)
			dynamic_count += !!(a8_powerup_ext[i].pipes & (1u << pipe));
	uint32_t needed = 4 + 2 * (A8_COUNT(a8_powerup_ifpc) + A8_COUNT(a8_powerup)) + 3 * dynamic_count;
	if (needed > powerup->size / 4 || A8_COUNT(a8_powerup_ifpc) > 255 || A8_COUNT(a8_powerup) > 255)
		return gpu_fail("powerup list capacity");
	memset(words, 0, powerup->size);
	if (!gpu_aperture(0))
		return false;
	for (unsigned i = 0; i < A8_COUNT(a8_powerup_ifpc); i++) {
		words[at++] = a8_powerup_ifpc[i];
		words[at++] = gpu_read(a8_powerup_ifpc[i]);
	}
	for (unsigned i = 0; i < A8_COUNT(a8_powerup); i++) {
		words[at++] = a8_powerup[i];
		words[at++] = gpu_read(a8_powerup[i]);
	}
	/* Catalog pipe support controls this list; LPAC dispatch stays disabled. */
	for (unsigned pipe = 1; pipe <= 7; pipe++) {
		if (!gpu_aperture(pipe))
			return false;
		for (unsigned i = 0; i < A8_COUNT(a8_powerup_ext); i++) {
			if (!(a8_powerup_ext[i].pipes & (1u << pipe)))
				continue;
			words[at++] = pipe << 12;
			words[at++] = a8_powerup_ext[i].reg;
			words[at++] = gpu_read(a8_powerup_ext[i].reg);
			dynamic++;
		}
	}
	if (!gpu_aperture(0))
		return false;
	words[3] = A8_COUNT(a8_powerup_ifpc) | (A8_COUNT(a8_powerup) << 8) | (dynamic << 16);
	if (at != needed || dynamic != dynamic_count || !adreno8_memory_publish(powerup, 0, at * 4))
		return gpu_fail("powerup list publication");
	printf("adreno8: GPU powerup list IOVA=%#x bytes=%u IFPC=%u preemption=%u dynamic=%u\n",
	    powerup->gpu_address, at * 4, (unsigned)A8_COUNT(a8_powerup_ifpc), (unsigned)A8_COUNT(a8_powerup), dynamic);
	return true;
}

static bool
gpu_register_config(void)
{
	if (!gpu_aperture(0))
		return false;
	/* Polling owns error handling during this cold-reset experiment. */
	gpu_write(GEN8_RBBM_INT_0_MASK, 0);
	gpu_write(GEN8_RBBM_PERFCTR_GPU_BUSY_MASKED, 0xffffffff);
	/* 12 MiB GMEM aligned to the catalog's 64 MiB UCHE base alignment. */
	gpu_write(GEN8_UCHE_CCHE_GC_GMEM_RANGE_MIN_LO, 0x04000000);
	gpu_write(GEN8_UCHE_CCHE_GC_GMEM_RANGE_MIN_HI, 0);
	gpu_write(GEN8_SP_HLSQ_GC_GMEM_RANGE_MIN_LO, 0x04000000);
	gpu_write(GEN8_SP_HLSQ_GC_GMEM_RANGE_MIN_HI, 0);
	gpu_write(GEN8_UCHE_TRAP_BASE_LO, 0xfffff000);
	gpu_write(GEN8_UCHE_TRAP_BASE_HI, 0x1ffff);
	gpu_write(GEN8_UCHE_WRITE_THRU_BASE_LO, 0xfffff000);
	gpu_write(GEN8_UCHE_WRITE_THRU_BASE_HI, 0x1ffff);
	adreno8_wr(adreno8_gmu.base + GMU(GEN8_GMUCX_POWER_COUNTER_SELECT_XOCLK_1), 4);
	/* sun-gpu.dtsi: UBWC 5, min-access-length 32; gen8_0_0 highest_bank_bit 16. */
	if (!gpu_pipe_write(2, GEN8_GRAS_NC_MODE_CNTL, 0x60) ||
	    !gpu_pipe_write(1, GEN8_GRAS_NC_MODE_CNTL, 0x60) ||
	    !gpu_pipe_write(1, GEN8_RB_CCU_NC_MODE_CNTL, 6) ||
	    !gpu_pipe_write(1, GEN8_RB_CMP_NC_MODE_CNTL, 0x20006) || !gpu_aperture(0))
		return false;
	gpu_write(GEN8_SP_NC_MODE_CNTL, 0x26);
	gpu_write(GEN8_TPL1_NC_MODE_CNTL, 6);
	for (unsigned i = 0; i < A8_COUNT(a8_bicubic); i++)
		gpu_write(a8_bicubic[i].reg, a8_bicubic[i].value);
	gpu_write(GEN8_UCHE_CLIENT_PF, 0x81);
	if (!gpu_noncontext())
		return false;
	gpu_write(GEN8_RBBM_INTERFACE_HANG_INT_CNTL, 0x40cfffff);
	gpu_write(GEN8_RBBM_SLICE_INTERFACE_HANG_INT_CNTL, 1u << 30);
	adreno8_wr(adreno8_gmu.base + GMU(GEN8_GMUAO_GPU_CX_BUSY_MASK), 0xff000000);
	uint32_t counter = adreno8_rd(adreno8_gmu.base + GMU(GEN8_GMUCX_POWER_COUNTER_SELECT_XOCLK_0));
	adreno8_wr(adreno8_gmu.base + GMU(GEN8_GMUCX_POWER_COUNTER_SELECT_XOCLK_0), (counter & ~0xffu) | 0x20);
	adreno8_wr(adreno8_gmu.base + GMU(GEN8_GMUCX_POWER_COUNTER_ENABLE), 1);
	if (!gpu_protect())
		return false;
	/* No LLCC slices are allocated; vendor LLCC helpers return in this case. */
	for (unsigned pipe = 1; pipe <= 7; pipe++) {
		if (!gpu_pipe_write(pipe, GEN8_CP_APRIV_CNTL_PIPE, pipe == 1 ? 0x6f : 0xf))
			return false;
		gpu_write(GEN8_CP_INTERRUPT_STATUS_MASK_PIPE, A8_CP_SW_MASK);
		gpu_write(GEN8_CP_HW_FAULT_STATUS_MASK_PIPE, A8_CP_HW_MASK);
	}
	if (!gpu_aperture(0))
		return false;
	gpu_write(GEN8_CP_INTERRUPT_STATUS_MASK_GLOBAL, A8_CP_GLOBAL_MASK);
	gpu_write(GEN8_RBBM_SECVID_TSB_CNTL, 0);
	gpu_write(GEN8_RBBM_SECVID_TSB_TRUSTED_BASE_LO, 0xf0000000);
	gpu_write(GEN8_RBBM_SECVID_TSB_TRUSTED_BASE_HI, 0);
	gpu_write(GEN8_RBBM_SECVID_TSB_TRUSTED_SIZE, 0xfffff000);
	if (!gpu_hwcg() || !gpu_powerup_list())
		return false;
	adreno8_settle();
	return gpu_status_ok();
}

static bool
gpu_ucode_base(uint32_t reg, const struct adreno8_memory *memory)
{
	gpu_write(reg, memory->gpu_address);
	gpu_write(reg + 1, 0);
	adreno8_settle();
	if (gpu_read(reg) != memory->gpu_address || gpu_read(reg + 1) != 0)
		return gpu_fail("microcode base readback");
	return true;
}

bool
adreno8_gpu_cp_init(void)
{
	uint32_t words[10];
	if (gpu_state != GPU_PREPARED)
		return gpu_fail("CP initialization order");
	if (!gpu_oob_get() || !gpu_register_config() ||
	    !gpu_ucode_base(GEN8_CP_SQE_INSTR_BASE_LO, sqe) ||
	    !gpu_ucode_base(GEN8_CP_AQE_INSTR_BASE_LO_0, aqe))
		return false;
	uint32_t count = adreno8_pm4_cp_init(words, A8_COUNT(words), powerup->gpu_address);
	if (!gpu_raw(words, count, "CP initialization"))
		return false;
	gpu_state = GPU_CP_READY;
	printf("adreno8: GPU CP initialization acknowledged and idle; zap authentication is next\n");
	return true;
}

bool
adreno8_gpu_cp_ready(void)
{
	return gpu_state == GPU_CP_READY && gpu_accessible();
}

bool
adreno8_gpu_secure_exit(void)
{
	uint32_t words[2];
	if (gpu_state != GPU_CP_READY)
		return gpu_fail("secure-mode transition order");
	uint32_t count = adreno8_pm4_secure_exit(words, A8_COUNT(words));
	if (!gpu_raw(words, count, "secure-mode transition"))
		return false;
	gpu_state = GPU_UNSECURE;
	return true;
}

bool
adreno8_gpu_scratch_test(void)
{
	uint32_t words[6];
	const uint32_t expected = 0x600dc0de;
	if (gpu_state != GPU_UNSECURE)
		return gpu_fail("scratch command order");
	uint32_t count = adreno8_pm4_scratch(words, A8_COUNT(words), scratch->gpu_address, expected);
	if (!gpu_raw(words, count, "scratch memory write") ||
	    !adreno8_memory_acquire(scratch, 0, scratch->size))
		return gpu_fail("scratch completion");
	volatile uint32_t *data = (volatile uint32_t *)scratch->memory;
	if (data[0] != expected) {
		printf("adreno8: GPU scratch actual=%#x expected=%#x\n", data[0], expected);
		return gpu_fail("scratch readback");
	}
	for (unsigned i = 1; i < scratch->size / 4; i++)
		if (data[i] != 0)
			return gpu_fail("scratch bounds");
	if (!gpu_status_ok())
		return false;
	gpu_state = GPU_TESTED;
	printf("adreno8: GPU CP wrote %#x to scratch; idle and SMMU checks passed; OOB and resources held until cold reset\n", expected);
	return true;
}
