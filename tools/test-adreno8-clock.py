#!/usr/bin/env python3
"""Execute production Sun PLL/RCG startup and failure paths with MMIO models."""
from pathlib import Path
import argparse
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "xnu/files/bsd/dev"

STUB = r'''
#ifndef _DEV_ADRENO8_PRIVATE_H
#define _DEV_ADRENO8_PRIVATE_H
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uintptr_t vm_offset_t;
struct adreno8_block { const char *name; uint32_t phys, size; vm_offset_t base; };
static uint32_t gpu[0x9800 / 4], gcc[0x80000 / 4];
struct adreno8_block adreno8_gpucc = {"gpucc", 0x3d90000, sizeof(gpu), (uintptr_t)gpu};
struct adreno8_block adreno8_gcc = {"gcc", 0x100000, sizeof(gcc), (uintptr_t)gcc};
static uint32_t adreno8_gmu_from_pll, adreno8_step, adreno8_newest_step;
static bool adreno8_reset_required, resources, pending_latch;
static unsigned reads, writes, resource_calls, running, elapsed_us;
static unsigned mode, drop_write, poison_read, pll_writes, rcg_updates;
static uint32_t write_only_high, cold_write_only_high;
static struct { bool is_gpu; uint32_t offset, value; } history[128];
enum { NORMAL, RESOURCE_FAIL, GPLL_UNLOCKED, GPLL_BAD_RATE, GPLL_GATE_FAIL,
       GPU_FSM, CONFIG_FAIL, GPU_UNLOCKED, GMU_FORCE_FAIL, GMU_UPDATE_FAIL,
       HUB_FORCE_FAIL, HUB_UPDATE_FAIL, LATCH_FAIL, POST_RATE_UNLOCKED,
       L_HIGH_IGNORED, L_HIGH_WRITE_ONLY };
#define GPUCC_GMU_RCGR 0x9318u
#define GPUCC_HUB_RCGR 0x93ecu
#define GPUCC_HUB_DIV 0x942cu
#define RCG_UPDATE 1u
static int adreno8_say(const char *format, ...) { (void)format; return 0; }
static void adreno8_need(struct adreno8_block *b) { assert(b->base); }
static void adreno8_settle(void) {}
static void adreno8_sleep_us(uint32_t us) {
    elapsed_us += us;
    if (pending_latch && mode!=LATCH_FAIL) gpu[0] |= 1u<<13;
    pending_latch=false;
}
static bool adreno8_rpmh_clock_prepare(void) {
    assert(!writes && !resource_calls); resource_calls++;
    resources = mode != RESOURCE_FAIL; return resources;
}
static uint32_t adreno8_rd(vm_offset_t address) {
    reads++; if (reads == poison_read) return UINT32_MAX;
    return *(uint32_t *)address;
}
static uint32_t adreno8_get(struct adreno8_block *b, const char *n, uint32_t off) {
    (void)n; assert(off < b->size && !(off & 3)); return adreno8_rd(b->base + off);
}
static void adreno8_put(struct adreno8_block *b, const char *n, uint32_t off, uint32_t value) {
    (void)n; assert(resources && adreno8_reset_required && writes < 128);
    bool is_gpu = b == &adreno8_gpucc;
    history[writes].is_gpu=is_gpu; history[writes].offset=off; history[writes].value=value;
    writes++; if (writes == drop_write) return;
    uint32_t *r=(uint32_t *)b->base, old=r[off/4];
    if (!is_gpu) {
        assert(off == 0x52020 || off == 0x52000);
        if (off == 0x52020) assert(value == (old | 1));
        if (off == 0x52000) { assert(value == (old | 0x8000)); if(mode==GPLL_GATE_FAIL) return; }
        r[off/4]=value; return;
    }
    assert(off != GPUCC_HUB_DIV);
    if (off < 0x34) pll_writes++;
    if (mode==CONFIG_FAIL && off==0x24) return;
    if (off == 0) {
        uint32_t status=old & ((1u<<31) | (1u<<13) | (1u<<8));
        r[0]=(value & ~((1u<<31) | (1u<<13) | (1u<<8))) | status;
        if (!(value & 4)) { r[0] &= ~(1u<<31); r[0] |= 1u<<8; }
        if (value & (1u<<14)) {
            pending_latch=true;
            if(mode==POST_RATE_UNLOCKED) r[0] &= ~(1u<<31);
        } else r[0] &= ~(1u<<13);
        if ((value & 1) && !(old & 1)) assert(old & (1u<<31));
    } else if (off == 0x10 && mode == L_HIGH_IGNORED) {
        r[4]=(old&0xff000000u)|(value&0x00ffffffu);
    } else if (off == 0x10 && mode == L_HIGH_WRITE_ONLY) {
        write_only_high=value>>24;
        if (!(old&0xffff)) cold_write_only_high=write_only_high;
        r[4]=value&0x00ffffffu;
    } else if (off == 4) {
        assert(value == 0 || value == 1); r[1]=value;
        if (value == 1) {
            assert(r[0]&4); assert((r[4]&0xffff)==0x43 && r[5]==0xb555);
            assert((r[6]&0x3c00)==0x400);
            running++;
            if(mode!=GPU_UNLOCKED) { r[0]|=1u<<31; r[0]&=~(1u<<8); }
        } else r[0] &= ~(1u<<31);
    } else if (off == GPUCC_GMU_RCGR || off == GPUCC_HUB_RCGR) {
        bool fail_force = mode == (off == GPUCC_GMU_RCGR ? GMU_FORCE_FAIL : HUB_FORCE_FAIL);
        bool fail_update = mode == (off == GPUCC_GMU_RCGR ? GMU_UPDATE_FAIL : HUB_UPDATE_FAIL);
        r[off/4]=value;
        if ((value&2) && !fail_force) r[off/4]&=~(1u<<31);
        if(value&1) { rcg_updates++; if(!fail_update) r[off/4]&=~1u; }
    } else r[off/4]=value;
}
#endif
'''

CASES = r'''
static void reset(unsigned next) {
    memset(gpu,0,sizeof(gpu)); memset(gcc,0,sizeof(gcc)); memset(history,0,sizeof(history));
    resources=adreno8_reset_required=pending_latch=false; adreno8_gmu_from_pll=true;
    adreno8_step=adreno8_newest_step=5;
    reads=writes=resource_calls=running=elapsed_us=drop_write=poison_read=pll_writes=rcg_updates=0;
    write_only_high=cold_write_only_high=0;
    mode=next; clock_state=CLOCK_COLD;
    gpu[0]=0x100; gpu[4]=0x440000; gpu[6]=1; gpu[7]=2;
    gpu[GPUCC_GMU_RCGR/4]=gpu[GPUCC_HUB_RCGR/4]=0x80040000;
    gpu[GPUCC_GMU_RCGR/4+1]=gpu[GPUCC_HUB_RCGR/4+1]=0x5030381f;
    gpu[GPUCC_HUB_DIV/4]=0x12340001;
    gcc[0]=(1u<<31)|5; gcc[1]=1; gcc[4]=0x4444001f; gcc[5]=0x4000; gcc[6]=1u<<25;
    gcc[0x52020/4]=0xa4000000; gcc[0x52000/4]=0x24000007;
    if(next==GPLL_UNLOCKED) gcc[0]&=~(1u<<31);
    if(next==GPLL_BAD_RATE) gcc[4]++;
    if(next==GPU_FSM) gpu[6]|=1u<<25;
}
static void closed(void) {
    unsigned r=reads,w=writes,c=resource_calls;
    assert(clock_state==CLOCK_FAILED && adreno8_reset_required);
    assert(!adreno8_clock_boot() && !adreno8_clock_started());
    assert(reads==r && writes==w && resource_calls==c);
}
static unsigned find(unsigned start, bool is_gpu, uint32_t off, uint32_t mask, uint32_t value) {
    for(unsigned i=start;i<writes;i++)
        if(history[i].is_gpu==is_gpu && history[i].offset==off && (history[i].value&mask)==value) return i;
    assert(!"required ordered register write absent"); return 0;
}
int main(void) {
    /* Model the observed L write 0x44440034 / read 0x00440034 mismatch. */
    reset(L_HIGH_IGNORED);
    assert(adreno8_clock_boot() && gpu[4]==0x00440043 && running==2);
    assert(adreno8_clock_started() && gpu[4]==0x00440034);
    reset(L_HIGH_IGNORED); gpu[4]=0x88440000;
    assert(adreno8_clock_boot() && gpu[4]==0x88440043 && running==2);
    reset(L_HIGH_WRITE_ONLY);
    assert(adreno8_clock_boot() && gpu[4]==0x00440043 && running==2 && cold_write_only_high==0x44);
    assert(adreno8_clock_started() && gpu[4]==0x00440034);
    reset(NORMAL); adreno8_gmu_from_pll=false;
    assert(adreno8_clock_boot() && adreno8_clock_started());
    assert(!reads && !writes && !resource_calls && !adreno8_reset_required);
    reset(NORMAL); assert(!adreno8_clock_started() && !writes && !reads); closed();
    reset(NORMAL); assert(adreno8_clock_boot());
    assert(clock_state==CLOCK_BOOT && running==2 && rcg_updates==2 && resource_calls==1);
    assert(gpu[4]==0x44440043 && gpu[5]==0xb555 && gpu[6]==0x407);
    assert(gpu[0]&1 && gpu[0]&4 && gpu[0]&(1u<<31));
    assert(gcc[0x52020/4]==0xa4000001 && gcc[0x52000/4]==0x24008007);
    assert(gcc[4]==0x4444001f && gcc[5]==0x4000);
    assert(gpu[GPUCC_GMU_RCGR/4+1]==((0x5030381f&~0x10371fu)|0x100201));
    assert(gpu[GPUCC_HUB_RCGR/4+1]==((0x5030381f&~0x10371fu)|0x100505));
    assert(gpu[GPUCC_HUB_DIV/4]==0x12340001 && gpu[GPUCC_GMU_RCGR/4]==0x40000);
    unsigned at=find(0,true,0x10,UINT32_MAX,0x44440034);
    at=find(at+1,true,0x14,UINT32_MAX,0x1555);
    at=find(at+1,true,0x20,UINT32_MAX,0x19660387);
    at=find(at+1,true,0x24,UINT32_MAX,0x098060a0);
    at=find(at+1,true,0x28,UINT32_MAX,0xb416cb20);
    at=find(at+1,true,0x18,UINT32_MAX,0x407);
    at=find(at+1,true,0x1c,UINT32_MAX,2);
    at=find(at+1,true,0x2c,UINT32_MAX,0);
    at=find(at+1,true,0x30,UINT32_MAX,0);
    at=find(at+1,false,0x52020,1,1);
    at=find(at+1,false,0x52000,0x8000,0x8000);
    at=find(at+1,true,0x10,0xffff,0x43);
    at=find(at+1,true,0x14,UINT32_MAX,0xb555);
    at=find(at+1,true,4,UINT32_MAX,1);
    at=find(at+1,true,0,1,1);
    at=find(at+1,true,0,1,0);
    at=find(at+1,true,4,UINT32_MAX,0);
    at=find(at+1,true,0,4,0);
    at=find(at+1,true,4,UINT32_MAX,1);
    at=find(at+1,true,0,1,1);
    at=find(at+1,true,GPUCC_GMU_RCGR,2,2);
    at=find(at+1,true,GPUCC_GMU_RCGR+4,0x10371f,0x100201);
    at=find(at+1,true,GPUCC_GMU_RCGR,1,1);
    at=find(at+1,true,GPUCC_GMU_RCGR,2,0);
    at=find(at+1,true,GPUCC_HUB_RCGR+4,0x10371f,0x100505);
    unsigned boot_writes=writes, boot_reads=reads;
    const unsigned rejected[] = {
        find(0,false,0x52020,1,1)+1, find(0,false,0x52000,0x8000,0x8000)+1,
        find(0,true,0x10,UINT32_MAX,0x44440034)+1,
        find(0,true,0x24,UINT32_MAX,0x098060a0)+1,
        find(0,true,0x10,0xffff,0x43)+1,
        find(0,true,4,UINT32_MAX,1)+1,
        find(0,true,0,1,1)+1,
        find(0,true,GPUCC_GMU_RCGR,2,2)+1,
        find(0,true,GPUCC_HUB_RCGR+4,0x10371f,0x100505)+1,
    };
    assert(adreno8_clock_boot() && writes==boot_writes && resource_calls==1);
    assert(adreno8_clock_started() && clock_state==CLOCK_RUNNING);
    assert(gpu[4]==0x44440034 && gpu[5]==0x1555 && !(gpu[0]&(1u<<14)));
    at=find(boot_writes,true,0x10,0xffff,0x34);
    at=find(at+1,true,0x14,UINT32_MAX,0x1555);
    at=find(at+1,true,0,1u<<14,1u<<14);
    find(at+1,true,0,1u<<14,0);
    assert(rcg_updates==2 && elapsed_us>=211);
    unsigned done_writes=writes;
    assert(adreno8_clock_started() && writes==done_writes);
    gpu[5]++; assert(!adreno8_clock_started()); closed();

    const unsigned limits[] = {0,4,5,6,14,16,17,18,23,UINT32_MAX};
    const unsigned phases[] = {0,4,5,6,13,14,15,17};
    for(unsigned i=0;i<sizeof(limits)/sizeof(*limits);i++)
    for(unsigned j=0;j<sizeof(phases)/sizeof(*phases);j++) {
        reset(NORMAL); adreno8_gmu_from_pll=2;
        adreno8_step=phases[j]; adreno8_newest_step=limits[i];
        bool admitted=phases[j]==5 && (limits[i]==5 || limits[i]>=17);
        assert(adreno8_clock_boot()==admitted);
        if(!admitted) { assert(!reads && !writes && !resource_calls); closed(); }
        reset(NORMAL); adreno8_gmu_from_pll=2; adreno8_newest_step=17;
        assert(adreno8_clock_boot());
        adreno8_step=phases[j]; adreno8_newest_step=limits[i];
        unsigned diagnostic_writes=writes, diagnostic_reads=reads;
        admitted=phases[j]==14 && limits[i]>=17;
        assert(adreno8_clock_started()==admitted);
        if(admitted) {
            assert(clock_state==CLOCK_RUNNING && gpu[4]==0x44440034);
            diagnostic_writes=writes;
            assert(adreno8_clock_started() && writes==diagnostic_writes);
        } else {
            assert(writes==diagnostic_writes && reads==diagnostic_reads); closed();
        }
    }
    reset(NORMAL); adreno8_gmu_from_pll=3; adreno8_step=14; adreno8_newest_step=17;
    clock_state=CLOCK_BOOT;
    assert(!adreno8_clock_started() && !reads && !writes); closed();
    reset(NORMAL); adreno8_gmu_from_pll=2; adreno8_newest_step=17;
    assert(adreno8_clock_boot()); adreno8_step=14; mode=LATCH_FAIL;
    assert(!adreno8_clock_started()); closed();
    for(unsigned fault=RESOURCE_FAIL;fault<=HUB_UPDATE_FAIL;fault++) {
        reset(fault); assert(!adreno8_clock_boot());
        if(fault==RESOURCE_FAIL) assert(!writes && !reads);
        if(fault<=GPLL_GATE_FAIL) assert(!running && !rcg_updates);
        if(fault==GPU_UNLOCKED) assert(!rcg_updates && elapsed_us>=1500);
        if(fault==GMU_FORCE_FAIL || fault==GMU_UPDATE_FAIL)
            assert(gpu[GPUCC_HUB_RCGR/4+1]==0x5030381f);
        closed();
    }
    for(unsigned fault=LATCH_FAIL;fault<=POST_RATE_UNLOCKED;fault++) {
        reset(NORMAL); assert(adreno8_clock_boot()); mode=fault;
        assert(!adreno8_clock_started()); closed();
    }
    for(unsigned i=0;i<sizeof(rejected)/sizeof(*rejected);i++) {
        reset(NORMAL); drop_write=rejected[i];
        assert(!adreno8_clock_boot()); closed();
    }
    reset(NORMAL); gcc[6]=7;
    assert(!adreno8_clock_boot() && !running && !rcg_updates && gcc[0x52020/4]==0xa4000000); closed();
    reset(NORMAL); gpu[4]=0x44440043; gpu[5]=0xb555; gpu[6]=0x80407;
    assert(adreno8_clock_boot() && gpu[4]==0x44440043 && gpu[6]==0x80407);
    for(unsigned i=0;i<writes;i++) assert(history[i].offset!=0x20 || !history[i].is_gpu);
    reset(NORMAL); gpu[4]=0x88440043; gpu[5]=0xb555;
    assert(adreno8_clock_boot() && running==2 && gpu[4]==0x88440043);
    const uint32_t unknown_cal[] = {0x88880000,0x44450000,0x45460000};
    for(unsigned i=0;i<sizeof(unknown_cal)/sizeof(*unknown_cal);i++) {
        reset(NORMAL); gpu[4]=unknown_cal[i]|0x43; gpu[5]=0xb555;
        assert(!adreno8_clock_boot() && !running && !rcg_updates);
        assert((gpu[4]&0xffff0000)==unknown_cal[i]); closed();
    }
    /* A live GMU using parent2 must divide by two before PLL0 changes rate. */
    reset(NORMAL); gpu[0]=0x80000005; gpu[1]=1; gpu[4]=0x44440034; gpu[5]=0x1555; gpu[6]=7;
    gpu[GPUCC_GMU_RCGR/4+1]=0x100201;
    assert(!adreno8_clock_boot() && !writes && !pll_writes && gpu[4]==0x44440034 && gpu[5]==0x1555); closed();
    reset(NORMAL); gpu[0]=0x80000005; gpu[1]=1; gpu[4]=0x44440034; gpu[5]=0x1555; gpu[6]=0x407;
    gpu[GPUCC_GMU_RCGR/4+1]=0x100201;
    assert(adreno8_clock_boot() && !running && gpu[4]==0x44440043);
    /* Calibration-complete hot inheritance never uses the retained calibration fields. */
    reset(NORMAL); gpu[0]=0x80000005; gpu[1]=1; gpu[4]=0x88880034; gpu[5]=0x1555; gpu[6]=0x407;
    gpu[GPUCC_GMU_RCGR/4+1]=0x100201;
    assert(adreno8_clock_boot() && !running && gpu[4]==0x88880043);
    assert(adreno8_clock_started() && !running && gpu[4]==0x88880034);
    reset(NORMAL); gpu[0]=0x80000005; gpu[1]=1; gpu[4]=0x44440043; gpu[5]=0xb555; gpu[6]=7;
    assert(adreno8_clock_boot() && !running && (gpu[6]&0x3c00)==0x400);
    for(unsigned parent=0;parent<8;parent++) {
        reset(NORMAL); gpu[6]=0x407; gpu[GPUCC_GMU_RCGR/4+1]=0x100001|(parent<<8);
        bool admitted=parent==0 || parent==2 || parent==5 || parent==6;
        assert(adreno8_clock_boot()==admitted);
        if(!admitted) { assert(!writes && !pll_writes && !rcg_updates); closed(); }
        reset(NORMAL); gpu[GPUCC_HUB_RCGR/4+1]=0x100005|(parent<<8);
        admitted=parent==0 || parent==5;
        assert(adreno8_clock_boot()==admitted);
        if(!admitted) { assert(!writes && !pll_writes && !rcg_updates); closed(); }
    }
    const uint32_t bad_hub[] = {0x501,0x504,0x506,0x1505,0x2505,0x3505};
    for(unsigned i=0;i<sizeof(bad_hub)/sizeof(*bad_hub);i++) {
        reset(NORMAL); gpu[GPUCC_HUB_RCGR/4+1]=0x100000|bad_hub[i];
        assert(!adreno8_clock_boot() && !writes && !pll_writes && !rcg_updates); closed();
    }
    reset(NORMAL); gpu[GPUCC_GMU_RCGR/4]=0x40002;
    assert(adreno8_clock_boot() && (gpu[GPUCC_GMU_RCGR/4]&2));
    /* Poison every boot read independently. All-ones values must close admission. */
    for(unsigned n=1;n<=boot_reads;n++) {
        reset(NORMAL); poison_read=n;
        bool ok=adreno8_clock_boot();
        if(ok) { fprintf(stderr,"survived all-ones read %u\n",n); return 2; }
        closed();
    }
    reset(NORMAL);
    puts("Sun clocks: ordered PLL calibration/650-to-500 latch, HUB parent, shared fields, faults and terminal retention: PASS");
    return 0;
}
'''


INIT_STUB = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
static uint32_t requested, selected, omitted, clock_mode;
static uint32_t keep_arg=1, return_arg;
static uint32_t empty_powerup_arg, raw_nop_arg, direct_ring_arg, secure_mode_arg;
static uint32_t adreno8_pause_ms, adreno8_watch_seconds, adreno8_speed_asked;
static uint32_t adreno8_gmu_from_pll, adreno8_gpu8_secure_mode, adreno8_fine;
static uint32_t adreno8_skip_steps, adreno8_newest_step, adreno8_step;
static bool adreno8_gpu8_keep, adreno8_gpu8_return, adreno8_gpu_stall, adreno8_cx_changed, adreno8_direct_ring;
static struct { uintptr_t base; } adreno8_gmu;
static unsigned calls, holds, stopped, returned, last_stage, checks;
static uint32_t visited;
#define SECURE_MODE_TRANSLATION_B 4
#define GMU_ALWAYS_ON_HI 0
#define GMU_ALWAYS_ON_LO 4
#define GMU_GENERAL_9 8
#define GMU_AO_INTERRUPT_STATUS 12
#define GMU_PWR_CLK_STATUS 16
static int init_log(const char *format, ...) { (void)format; return 0; }
#define printf init_log
static bool PE_parse_boot_argn(const char *key, void *out, unsigned size) {
    assert(size==4);
    uint32_t value;
    if(!strcmp(key,"adreno8")) value=requested;
    else if(!strcmp(key,"adreno8_only")) value=selected;
    else if(!strcmp(key,"adreno8_skip")) value=omitted;
    else if(!strcmp(key,"adreno8_gmuclk")) value=clock_mode;
    else if(!strcmp(key,"adreno8_keep")) value=keep_arg;
    else if(!strcmp(key,"adreno8_return")) value=return_arg;
    else if(!strcmp(key,"adreno8_empty_powerup")) value=empty_powerup_arg;
    else if(!strcmp(key,"adreno8_raw_nop")) value=raw_nop_arg;
    else if(!strcmp(key,"adreno8_direct_ring")) value=direct_ring_arg;
    else if(!strcmp(key,"adreno8_secure_preflight")) value=secure_mode_arg;
    else if(!strcmp(key,"adreno8_watch")) value=0;
    else return false;
    memcpy(out,&value,sizeof(value)); return true;
}
static bool phase(unsigned number) {
    assert(number==adreno8_step && number>last_stage && number<=23);
    last_stage=number; visited|=1u<<(number-1); calls++; return true;
}
static void adreno8_hold(bool fine) { assert(!fine); holds++; }
static void adreno8_smmu_cb_scan_ask(void) { assert(false); }
static void adreno8_sleep_ms(uint32_t ms) { (void)ms; assert(false); }
static uint32_t adreno8_rd(uintptr_t address) { (void)address; assert(false); return 0; }
static void adreno8_gpu8_block_stop(void) { stopped++; }
static void loader_return(void) { returned++; }
'''

INIT_CASES = r'''
#undef printf
static void check(uint32_t mode, uint32_t asked, uint32_t only, uint32_t skip, uint32_t expected) {
    requested=asked; selected=only; omitted=skip; clock_mode=mode;
    adreno8_gmu_from_pll=adreno8_skip_steps=adreno8_newest_step=adreno8_step=0;
    adreno8_gpu8_keep=adreno8_gpu8_return=adreno8_cx_changed=false;
    adreno8_gpu8_secure_mode=0; adreno8_direct_ring=false;
    calls=holds=stopped=returned=last_stage=visited=0;
    adreno8_init();
    unsigned count=0;
    for(unsigned bit=0;bit<23;bit++) count+=(expected>>bit)&1;
    assert(visited==expected && calls==count && holds==count);
    assert(stopped==(mode<=1 && asked && !keep_arg && last_stage>=3));
    assert(returned==(mode<=1 && expected && return_arg));
    checks++;
}
int main(void) {
    check(2,5,0,0,0x1f);
    check(2,17,0,0,0x1ffff);
    check(2,18,0,0,0x3ffff); check(2,23,0,0,0x7fffff); check(2,UINT32_MAX,0,0,0x7fffff);
    const uint32_t rejected[][3]={
        {5,14,0}, {5,5,0}, {5,0,1u<<4}, {14,0,0}, {14,5,0},
        {4,0,0}, {0,0,0}, {5,0,1u<<31},
        {17,5,0}, {17,17,0}, {17,0,1u<<13}, {17,0,1u<<31},
        {16,0,0}
    };
    for(unsigned i=0;i<sizeof(rejected)/sizeof(*rejected);i++)
        check(2,rejected[i][0],rejected[i][1],rejected[i][2],0);
    for(unsigned keep=0;keep<2;keep++) for(unsigned back=0;back<2;back++) {
        keep_arg=keep; return_arg=back;
        check(2,5,0,0,keep && !back ? 0x1f : 0);
        check(2,17,0,0,keep && !back ? 0x1ffff : 0);
    }
    keep_arg=1; return_arg=0;
    for(unsigned mode=0;mode<=1;mode++) {
        check(mode,5,0,0,0x1f);
        check(mode,14,0,0,0x3fff);
        check(mode,5,14,0,1u<<13);
        check(mode,14,5,0,1u<<4);
        check(mode,14,0,1u<<4,0x3fff&~(1u<<4));
        check(mode,UINT32_MAX,0,0,0x7fffff);
        check(mode,5,0,1u<<31,0x1f);
        check(mode,0,0,0,0);
        for(unsigned keep=0;keep<2;keep++) for(unsigned back=0;back<2;back++) {
            keep_arg=keep; return_arg=back;
            check(mode,5,0,0,0x1f);
            check(mode,17,0,0,0x1ffff);
        }
        keep_arg=1; return_arg=0;
    }
    for(unsigned mode=0;mode<=2;mode++) {
        empty_powerup_arg=1;
        check(mode,17,0,0,0x1ffff);
        const uint32_t bad_targets[]={0,5,16,18,23,UINT32_MAX};
        for(unsigned i=0;i<sizeof(bad_targets)/sizeof(*bad_targets);i++)
            check(mode,bad_targets[i],0,0,0);
        check(mode,17,17,0,0);
        check(mode,17,0,1u<<5,0);
        empty_powerup_arg=2; check(mode,17,0,0,0);
        empty_powerup_arg=UINT32_MAX; check(mode,17,0,0,0);
        empty_powerup_arg=1;
        for(unsigned raw=1;raw<=4;raw++) {
            raw_nop_arg=raw; check(mode,17,0,0,0);
        }
        raw_nop_arg=0;
        for(unsigned keep=0;keep<2;keep++) for(unsigned back=0;back<2;back++) {
            keep_arg=keep; return_arg=back;
            check(mode,17,0,0,keep && !back ? 0x1ffff : 0);
        }
        keep_arg=1; return_arg=0; empty_powerup_arg=0;
        raw_nop_arg=1; check(mode,17,0,0,0x1ffff); raw_nop_arg=0;
    }
    direct_ring_arg=1;
    check(2,17,0,0,0x1ffff); assert(adreno8_direct_ring);
    const uint32_t direct_bad_targets[]={0,5,14,16,18,22,23,UINT32_MAX};
    for(unsigned i=0;i<sizeof(direct_bad_targets)/sizeof(*direct_bad_targets);i++)
        check(2,direct_bad_targets[i],0,0,0);
    check(2,17,17,0,0); check(2,17,0,1u<<5,0); check(2,17,0,1u<<31,0);
    direct_ring_arg=2; check(2,17,0,0,0); direct_ring_arg=UINT32_MAX; check(2,17,0,0,0);
    direct_ring_arg=1;
    for(unsigned mode=0;mode<2;mode++) check(mode,17,0,0,0);
    for(unsigned mode=3;mode<5;mode++) check(mode,17,0,0,0);
    for(unsigned value=1;value<=5;value++) {
        raw_nop_arg=value; check(2,17,0,0,value == 2 || value == 3 ? 0x1ffff : 0); raw_nop_arg=0;
        empty_powerup_arg=value; check(2,17,0,0,0); empty_powerup_arg=0;
        secure_mode_arg=value; check(2,17,0,0,0); secure_mode_arg=0;
    }
    secure_mode_arg=UINT32_MAX; check(2,17,0,0,0); secure_mode_arg=0;
    for(unsigned keep=0;keep<2;keep++) for(unsigned back=0;back<2;back++) {
        keep_arg=keep; return_arg=back;
        check(2,17,0,0,keep && !back ? 0x1ffff : 0);
    }
    keep_arg=1; return_arg=0; direct_ring_arg=0;
    check(2,17,0,0,0x1ffff); assert(!adreno8_direct_ring);
    printf("Sun init admission: %u production cases PASS\n",checks);
    return 0;
}
'''


def vendor_check(directory):
    gpu = (directory / "drivers/clk/qcom/gpucc-sun.c").read_text()
    gcc = (directory / "drivers/clk/qcom/gcc-sun.c").read_text()
    pll = (directory / "drivers/clk/qcom/clk-alpha-pll.c").read_text()
    hdr = (directory / "drivers/clk/qcom/clk-alpha-pll.h").read_text()
    for value in (".l = 0x34", ".alpha = 0x1555", "0x19660387", "0x098060a0", "0xb416cb20",
                  "F(500000000, P_GPU_CC_PLL0_OUT_EVEN, 1", "F(650000000, P_GPU_CC_PLL0_OUT_EVEN, 1",
                  "F(200000000, P_GPLL0_OUT_MAIN, 3", ".reg = 0x942c", "clk_regmap_div_ro_ops"):
        assert value in gpu, value
    gate = re.search(r"static struct clk_branch gcc_gpu_gpll0_clk_src = \{.*?\n};", gcc, re.S).group()
    assert "0x52000" in gate and "BIT(15)" in gate and "BRANCH_HALT_DELAY" in gate
    assert "#define clk_alpha_pll_taycan_elu_ops clk_alpha_pll_lucid_evo_ops" in hdr
    assert "LUCID_5LPE_ALPHA_PLL_ACK_LATCH\tBIT(13)" in pll
    assert "LUCID_5LPE_PLL_LATCH_INPUT\tBIT(14)" in pll


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor-dir", type=Path, required=True)
    args = parser.parse_args()
    vendor_check(args.vendor_dir)
    production = (DEV / "adreno8_clock.c").read_text()
    with tempfile.TemporaryDirectory(prefix="adreno8-clock-") as temp:
        path = Path(temp)
        (path / "adreno8_private.h").write_text(STUB)
        (path / "adreno8_rpmh_private.h").write_text('#include "adreno8_private.h"\n')
        (path / "adreno8_clock_private.h").write_text((DEV / "adreno8_clock_private.h").read_text())
        def run(label, content, success=True, cases=CASES):
            src, binary = path / (label + ".c"), path / label
            src.write_text(content + cases)
            subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                            "-fno-omit-frame-pointer", "-I", str(path), str(src), "-o", str(binary)],
                           check=True, capture_output=True, text=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            if success and result.returncode:
                raise RuntimeError(label + ":\n" + result.stdout + result.stderr)
            if not success and not result.returncode:
                raise RuntimeError("mutation survived: " + label)
            if success:
                print(result.stdout, end="")

        run("check", production)
        mutations = [
            ("ignored-resource-failure", 'if (!adreno8_rpmh_clock_prepare())',
             '(void)adreno8_rpmh_clock_prepare(); if (false)'),
            ("high-L-overwritten", '"PLL0 L rate", PLL_L, 0xffff, l', '"PLL0 L rate", PLL_L, 0xffffffffu, l'),
            ("old-latch-bit", '#define PLL_LATCH (1u << 14)', '#define PLL_LATCH (1u << 22)'),
            ("no-latch-delay", 'adreno8_sleep_us(1);\n\t\tuint32_t ack', 'uint32_t ack'),
            ("HUB-from-GPU-PLL", '#define RCG_HUB_CONFIG 0x100505u', '#define RCG_HUB_CONFIG 0x100205u'),
            ("no-HW-clock-control", '#define RCG_GMU_CONFIG 0x100201u', '#define RCG_GMU_CONFIG 0x000201u'),
            ("wrong-parent-rate", 'pll_frequency(&adreno8_gcc, 600000000)', 'pll_frequency(&adreno8_gcc, 500000000)'),
            ("ignored-update-timeout", 'if (!clock_poll(&adreno8_gpucc, command, RCG_UPDATE, 0, 500)) return false;',
             '(void)clock_poll(&adreno8_gpucc, command, RCG_UPDATE, 0, 500);'),
            ("retry-after-failure", 'clock_state = CLOCK_FAILED;', 'clock_state = CLOCK_COLD;'),
            ("no-inherited-even-check", 'if (gmu_parent == 2 && (user & 0x3c00) != 0x400)', 'if (false)'),
            ("no-inherited-calibration-check", 'if ((l & 0x00ff0000u) != 0x00440000u)', '(void)l; if (false)'),
            ("no-inherited-GMU-parent-check", 'if (gmu_parent != 0 && gmu_parent != 2 && gmu_parent != 5 && gmu_parent != 6)', 'if (false)'),
            ("no-inherited-HUB-range-check", 'if (hub_parent != 0 && (hub_source & 0x371f) != 0x505)',
             '(void)hub_parent; if (false)'),
            ("diagnostic-wrong-stage", 'adreno8_step != 5 ||', ''),
            ("diagnostic-wrong-limit", '(adreno8_newest_step != 5 && adreno8_newest_step < 17)', 'false'),
            ("diagnostic-START-wrong-limit", 'adreno8_newest_step >= 17 && ', ''),
            ("diagnostic-START-wrong-phase", ' && adreno8_step == 14', ''),
            ("diagnostic-START-unknown-mode", '!(adreno8_gmu_from_pll == 2 &&', '!(true &&'),
            ("parent-gate-before-source-check", '!pll_configure() || !hub_parent()', '!hub_parent() || !pll_configure()'),
            ("full-L-readback", '0x00ffffffu, 0x00440034u', '0xffffffffu, 0x44440034u'),
            ("omitted-process-calibration-write", '{PLL_L, 0x44440034}', '{PLL_L, 0x00440034}'),
            ("wide-calibration-guard", 'if ((l & 0x00ff0000u) != 0x00440000u)',
             'if ((l & 0xffff0000u) != 0x44440000u)'),
        ]
        for label, before, after in mutations:
            assert before in production, label
            run(label, production.replace(before, after, 1), success=False)
        print("Sun clock mutations rejected:", len(mutations))

        init = re.search(r"\nvoid\nadreno8_init\(void\)\n\{.*?\n\}",
                         (DEV / "adreno8.c").read_text(), re.S).group()
        entries = init.split("each[])(void) = {", 1)[1].split("};", 1)[0]
        names = re.findall(r"\badreno8_\w+\b", entries)
        assert len(names) == 23 and len(set(names)) == 23
        phases = "\n".join(f"static bool {name}(void) {{ return phase({i}); }}"
                           for i, name in enumerate(names, 1))
        run("init-admission", INIT_STUB + phases + init, cases=INIT_CASES)
        admission_mutations = [
            ("asked", "(asked != 5 && asked < 17) || ", ""),
            ("only", "only != 0 ||", ""),
            ("skip", "adreno8_skip_steps != 0", "false"),
            ("keep", " || !adreno8_gpu8_keep", ""),
            ("return", " || adreno8_gpu8_return", ""),
        ]
        clock_guard = re.search(r"\tif \(adreno8_gmu_from_pll == 2.*?\n\t}", init, re.S)[0]
        for label, before, after in admission_mutations:
            assert clock_guard.count(before) == 1, label
            changed = init.replace(clock_guard, clock_guard.replace(before, after, 1), 1)
            run("init-admission-" + label, INIT_STUB + phases + changed,
                success=False, cases=INIT_CASES)
        print("Sun init admission mutations rejected:", len(admission_mutations))

        empty_guard = re.search(r"\tif \(empty_powerup.*?\n\t}", init, re.S)[0]
        empty_mutations = [
            ("value", "empty_powerup != 1 || ", ""),
            ("asked", "asked != 17 || ", ""),
            ("only", "only != 0 ||", ""),
            ("skip", "adreno8_skip_steps != 0 || ", ""),
            ("keep", "!adreno8_gpu8_keep || ", ""),
            ("return", "adreno8_gpu8_return || ", ""),
            ("raw", "raw_nop != 0", "false"),
        ]
        for label, before, after in empty_mutations:
            assert empty_guard.count(before) == 1, label
            changed = init.replace(empty_guard, empty_guard.replace(before, after, 1), 1)
            run("init-empty-powerup-" + label, INIT_STUB + phases + changed,
                success=False, cases=INIT_CASES)
        print("Empty powerup init admission mutations rejected:", len(empty_mutations))

        direct_guard = re.search(r"\tif \(direct_ring.*?\n\t}", init, re.S)[0]
        direct_mutations = [
            ("value", "direct_ring != 1 || ", ""),
            ("asked", "asked != 17 || ", ""),
            ("only", "only != 0 ||", ""),
            ("skip", "adreno8_skip_steps != 0 || ", ""),
            ("keep", "!adreno8_gpu8_keep || ", ""),
            ("return", "adreno8_gpu8_return || ", ""),
            ("raw", "(raw_nop != 0 && raw_nop != 2 && raw_nop != 3) ||", ""),
            ("empty", "empty_powerup != 0 ||", ""),
            ("secure", "secure_mode_arg != 0 ||", ""),
            ("clock", "adreno8_gmu_from_pll != 2", "false"),
        ]
        direct_cases = (INIT_CASES.split("int main(void) {", 1)[0] +
                        "int main(void) {\n    direct_ring_arg=1;" + INIT_CASES.split("    direct_ring_arg=1;", 1)[1])
        # Isolate direct-mode cases so the independent clock guard cannot hide a removed restriction.
        for label, before, after in direct_mutations:
            assert direct_guard.count(before) == 1, label
            changed = init.replace(direct_guard, direct_guard.replace(before, after, 1), 1)
            if label in ("asked", "only", "skip", "keep", "return"):
                changed = changed.replace(clock_guard, clock_guard.replace("adreno8_gmu_from_pll == 2", "false"), 1)
            if label == "secure":
                changed = changed.replace("secure_mode_arg = one;", "secure_mode_arg = one; (void)secure_mode_arg;", 1)
            run("init-direct-ring-" + label, INIT_STUB + phases + changed,
                success=False, cases=direct_cases)
        print("Direct ring init admission mutations rejected:", len(direct_mutations))



if __name__ == "__main__":
    main()
