#!/usr/bin/env python3
"""Execute production ACTIVE CX/MX preparation, retaining all uncertain transactions."""
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "xnu/files/bsd/dev"

MODEL = r'''
#include "adreno8_rpmh.c"
#undef printf
static uint32_t regs[0x10000 / 4], before[0x10000 / 4];
static uint32_t inherited[2], votes[2], addresses[2] = {0x30000, 0x30010};
static unsigned scenario, fail_at, reads, writes, triggers, responses, lookups, tx_polls, voltage_writes;
static bool triggered, done, tx_write, poisoned_poll;
static unsigned tx_resource;
static uint32_t initial_control = ARC_AMC_ENABLE, initial_irq = 1;
static bool PE_parse_boot_argn(const char *key, void *out, unsigned size) {
    assert(size == 4); *(uint32_t *)out = 0;
    if (!strcmp(key, "adreno8_arc_read")) { *(uint32_t *)out = 1; return true; }
    unsigned n = !strcmp(key, "touchscreen") ? 0 : !strcmp(key, "wfi") ? 1 : 2;
    assert(n != 2 || !strcmp(key, "wfe_mode"));
    if (scenario == 3 + 2 * n) return false;
    if (scenario == 4 + 2 * n) *(uint32_t *)out = 1;
    return true;
}
static void adreno8_settle(void) {}
static void adreno8_sleep_us(uint32_t us) { assert(us == 100); }
static uint32_t adreno8_rpmh_lookup(const char *name, uint16_t *levels, unsigned *count,
                                  const uint8_t **data, unsigned *length) {
    assert(!data && !length);
    unsigned rail = !strcmp(name, "cx.lvl") ? 0 : 1;
    assert(!rail || !strcmp(name, "mx.lvl")); lookups++;
    const uint16_t standard[] = {0,16,64,128,192,256,320,384,416,448,480};
    memcpy(levels, standard, sizeof(standard)); *count = rail ? 10 : 11;
    if (scenario == 27) *count = 0;
    if (scenario == 28) *count = 65;
    if (scenario == 29) levels[4] = levels[3];
    if (scenario == 30) *count = 2;
    if (scenario == 34) { levels[2]=32; levels[3]=64; levels[4]=128; }
    if ((scenario == 25 && !rail) || (scenario == 26 && rail)) return 0;
    if (scenario == 52) return 0x40000;
    if (scenario == 53) return 0x30001;
    if (scenario == 38) return 0x30000;
    return addresses[rail];
}
static uint32_t adreno8_get(struct adreno8_block *block, const char *name, uint32_t off) {
    assert(!poisoned_poll);
    assert(block->phys == 0x16520000 && block->size == 0x10000 && off < 0x10000 && !(off & 3));
    assert(off == 0 || off == 4 || off == 0xc || off == 0xd28 || off == 0x1268 ||
        off == ARC_CONTROL || off == ARC_STATUS || off == ARC_ENABLE || off == ARC_WAIT ||
        off == ARC_MSGID || off == ARC_ADDRESS || off == ARC_DATA || off == ARC_COMMAND_STATUS ||
        off == ARC_RESPONSE || off == ARC_IRQ_ENABLE || off == ARC_IRQ_STATUS ||
        (off >= 0x1500 && off <= 0x1f8c && ((off - 0xd00) % 0x2a0 >= 0x20) &&
         ((off - 0xd00) % 0x2a0 <= 0x2c)));
    block->base = 0x100000; reads++;
    if ((scenario == 59 && !strcmp(name, "irq enable before restore")) ||
        (scenario == 60 && triggers == 3 && !strcmp(name, "irq status after request")) ||
        (scenario == 61 && triggers == 3 && off == ARC_COMMAND_STATUS) ||
        (scenario == 63 && triggers == 3 && !strcmp(name, "irq enable before restore")))
        return UINT32_MAX;
    if (scenario == 48 && triggers == 2 && !strcmp(name, "irq enable before request"))
        regs[ARC_IRQ_ENABLE / 4] |= 1u << 5;
    if (scenario == 49 && !strcmp(name, "irq enable before restore"))
        regs[ARC_IRQ_ENABLE / 4] |= 1u << 5;
    if (scenario == 51 && triggers == 3 && !strcmp(name, "TCS1 control after completion"))
        regs[ARC_CONTROL / 4] |= 2;
    if (scenario == 57 && triggers == 2 && off == 4) regs[1] |= 1u << 24;
    if (off == ARC_RESPONSE) { assert(done && !tx_write); responses++; }
    return regs[off / 4];
}
static void adreno8_put(struct adreno8_block *block, const char *name, uint32_t off, uint32_t value) {
    (void)name;
    assert(!poisoned_poll && adreno8_reset_required && block->phys == 0x16520000);
    assert(off == ARC_CONTROL || off == ARC_ENABLE || off == ARC_MSGID || off == ARC_ADDRESS ||
        off == ARC_DATA || off == ARC_WAIT || off == ARC_IRQ_ENABLE || off == ARC_IRQ_CLEAR);
    assert(!triggered || done || (off == ARC_CONTROL && value == (ARC_AMC_ENABLE | ARC_AMC_TRIGGER)));
    writes++;
    if (off == ARC_DATA) {
        unsigned rail = regs[ARC_ADDRESS / 4] == addresses[0] ? 0 : 1;
        unsigned required = scenario == 34 ? 4 : 3;
        if (required < inherited[rail]) required = inherited[rail];
        assert(responses >= 2 && value == required && value >= inherited[rail]);
        voltage_writes++;
    }
    if (off == ARC_WAIT) assert(value == 0 || value == 1);
    if (off == ARC_MSGID) assert(value == 0x108 || value == 0x10108);
    if (off == ARC_ADDRESS) assert(value == 0x30000 || value == 0x30010);
    if (fail_at == writes) { regs[off / 4] = value ^ 1; return; }
    if (off == ARC_IRQ_CLEAR) {
        assert(done && value == ARC_TCS_BIT);
        if (!(scenario == 50 && triggers == 3)) regs[ARC_IRQ_STATUS / 4] &= ~value;
        return;
    }
    if (off == ARC_CONTROL && value == (ARC_AMC_ENABLE | ARC_AMC_TRIGGER)) {
        assert(regs[ARC_CONTROL / 4] == ARC_AMC_ENABLE && !(regs[ARC_IRQ_STATUS / 4] & ARC_TCS_BIT));
        assert(regs[ARC_IRQ_ENABLE / 4] & ARC_TCS_BIT);
        tx_write = regs[ARC_MSGID / 4] == 0x10108;
        assert(regs[ARC_ENABLE / 4] == 1 && regs[ARC_WAIT / 4] == unsigned(tx_write));
        tx_resource = regs[ARC_ADDRESS / 4] == addresses[0] ? 0 : 1;
        triggered = true; done = false; tx_polls = 0; triggers++;
    }
    regs[off / 4] = value;
}
static uint32_t adreno8_rd(vm_offset_t address) {
    assert(address == 0x100000 + ARC_IRQ_STATUS && triggered); reads++; tx_polls++;
    if (scenario == 62 && triggers == 3) { poisoned_poll=true; return UINT32_MAX; }
    bool timeout = scenario >= 40 && scenario <= 45 && triggers == scenario - 39;
    if (tx_polls == 3 && !timeout) {
        done = true;
        if (tx_write) votes[tx_resource] = regs[ARC_DATA / 4];
        if (scenario == 47 && tx_write) votes[tx_resource]--;
        regs[ARC_IRQ_STATUS / 4] |= ARC_TCS_BIT | 4;
        regs[ARC_COMMAND_STATUS / 4] = scenario == 46 && triggers == 3 ? 1u << 8 : ARC_COMMAND_DONE;
        regs[ARC_STATUS / 4] = 1;
        regs[ARC_RESPONSE / 4] = scenario == 58 && triggers == 4 ? 11 : votes[tx_resource];
    }
    return regs[ARC_IRQ_STATUS / 4];
}
static void setup(void) {
    assert(ARC_WAIT == 0xfc0 && ARC_CONTROL == 0xfc4 && ARC_STATUS == 0xfc8 && ARC_ENABLE == 0xfcc);
    assert(ARC_MSGID == 0xfd4 && ARC_ADDRESS == 0xfd8 && ARC_DATA == 0xfdc);
    assert(ARC_COMMAND_STATUS == 0xfe0 && ARC_RESPONSE == 0xfe4 && ARC_TCS_BIT == 2);
    adreno8_gmu_from_pll = 1;
    regs[0]=0x30800; regs[1]=0x04010100; regs[3]=0x800c8104;
    regs[ARC_CONTROL / 4]=initial_control; regs[ARC_STATUS / 4]=1;
    regs[0xd28 / 4]=regs[0x1268 / 4]=1;
    regs[ARC_IRQ_ENABLE / 4]=initial_irq; regs[ARC_DATA / 4]=0xdeadbeef;
    for(unsigned i=3;i<8;i++) regs[(0xd00+i*0x2a0+0x28)/4]=1;
    switch(scenario) {
    case 1: adreno8_gmu_from_pll=0; break;
    case 2: adreno8_gmu_from_pll=3; break;
    case 9: regs[0]=0x30700; break;
    case 10: regs[1]=UINT32_MAX; break;
    case 11: regs[1]|=1u<<24; break;
    case 12: regs[3]=0; break;
    case 13: regs[(0xd00+3*0x2a0+0x2c)/4]=1; break;
    case 14: regs[(0xd00+4*0x2a0+0x20)/4]=1; break;
    case 15: regs[(0xd00+5*0x2a0+0x24)/4]=1; break;
    case 16: regs[(0xd00+7*0x2a0+0x28)/4]=0; break;
    case 17: regs[0xd28/4]=0; break;
    case 18: regs[0x1268/4]=0; break;
    case 19: regs[ARC_STATUS/4]=0; break;
    case 20: regs[ARC_ENABLE/4]=1; break;
    case 21: regs[ARC_WAIT/4]=1; break;
    case 22: regs[ARC_CONTROL/4]|=ARC_AMC_TRIGGER; break;
    case 23: regs[ARC_IRQ_STATUS/4]=ARC_TCS_BIT; break;
    case 24: regs[ARC_IRQ_ENABLE/4]=UINT32_MAX; break;
    case 31: inherited[0]=7; inherited[1]=6; break;
    case 32: inherited[0]=7; break;
    case 33: inherited[1]=7; break;
    case 35: inherited[0]=inherited[1]=3; break;
    case 36: inherited[0]=11; break;
    case 37: inherited[1]=10; break;
    }
    memcpy(votes,inherited,sizeof(votes)); memcpy(before,regs,sizeof(regs));
}
int main(int argc,char **argv) {
    assert(argc>=2); scenario=atoi(argv[1]); if(argc>2)fail_at=atoi(argv[2]); setup();
    if(scenario==55) assert(adreno8_rpmh_arc_read() && arc_state==ARC_READ_COMPLETE);
    bool result=adreno8_rpmh_clock_prepare();
    bool success=scenario==0 || scenario==1 || (scenario>=31 && scenario<=35) || scenario==49 || scenario==55;
    if(fail_at) assert(!result && writes==fail_at && adreno8_reset_required);
    else assert(result==success);
    if(scenario==1) assert(!reads && !writes && !lookups);
    if(result && scenario!=1) {
        unsigned target=scenario==34?4:3;
        for(unsigned i=0;i<2;i++) assert(votes[i]==(inherited[i]>target?inherited[i]:target));
        unsigned expected_writes=(inherited[0]<target)+(inherited[1]<target);
        assert(voltage_writes==expected_writes && adreno8_reset_required);
        assert(regs[ARC_CONTROL/4]==initial_control && !regs[ARC_ENABLE/4] && !regs[ARC_WAIT/4]);
        assert(regs[ARC_IRQ_ENABLE/4]==(initial_irq | (scenario==49?1u<<5:0)));
        assert(regs[ARC_IRQ_STATUS/4]==4);
    }
    if(!result && !fail_at && scenario<=28) assert(!voltage_writes);
    if(scenario==40 || scenario==41 || scenario==42 || scenario==43 || scenario==44 || scenario==45)
        assert(triggers==scenario-39 && tx_polls==100000 && !done);
    if(scenario==59) assert(writes==13 && triggers==1 && !voltage_writes);
    if(scenario==60 || scenario==61 || scenario==62) assert(writes==38 && triggers==3 && voltage_writes==1);
    if(scenario==62) assert(tx_polls==1 && !done);
    if(scenario==63) assert(writes==44 && triggers==3 && voltage_writes==1);
    for(unsigned i=0;i<0x10000/4;i++) {
        unsigned off=4*i;
        bool tcs1=off>=ARC_WAIT && off<=ARC_RESPONSE;
        if(off!=ARC_IRQ_ENABLE && off!=ARC_IRQ_STATUS && !tcs1 &&
           !(scenario==57 && off==4)) assert(regs[i]==before[i]);
    }
    unsigned old_reads=reads, old_writes=writes;
    if(scenario!=1) {
        assert(!adreno8_rpmh_clock_prepare());
        if(scenario!=2) assert(!adreno8_rpmh_arc_read());
    } else assert(adreno8_rpmh_clock_prepare());
    assert(reads==old_reads && writes==old_writes);
}
'''


HIGHEST_MODEL = r'''
#include "adreno8_rpmh.c"
#undef printf
static uint32_t regs[0x10000/4], before[0x10000/4], votes[2];
static uint8_t raw[2][34];
static unsigned scenario, fail_at, reads, writes, lookups, triggers, voltage_writes, polls, tx_polls;
static bool done, triggered, poison, prior_read;
static unsigned rail;
static bool PE_parse_boot_argn(const char *name,void *out,unsigned size) {
    assert(size==4); *(uint32_t *)out=0;
    if(!strcmp(name,"adreno8_arc_read")) { *(uint32_t *)out=1; return true; }
    unsigned n=!strcmp(name,"touchscreen")?0:!strcmp(name,"wfi")?1:2;
    assert(n!=2 || !strcmp(name,"wfe_mode"));
    if(scenario==3+2*n) return false;
    if(scenario==4+2*n) *(uint32_t *)out=1;
    return true;
}
static void adreno8_settle(void) {}
static void adreno8_sleep_us(uint32_t us) { assert(us==100); }
static uint32_t adreno8_rpmh_lookup(const char *name,uint16_t *levels,unsigned *count,
                                  const uint8_t **data,unsigned *length) {
    unsigned r=!strcmp(name,"cx.lvl")?0:1;
    assert(!r || !strcmp(name,"mx.lvl")); assert(lookups++==r);
    const uint16_t values[]={0,16,64,128,192,256,320,384,416,448,480};
    memcpy(levels,values,sizeof(values)); *count=r?10:11;
    if((scenario==22 && !r) || (scenario==23 && r)) *count=0;
    if(scenario==24 && !r) levels[10]--;
    if(scenario==25 && r) levels[9]--;
    if(scenario==26 && r) levels[4]--;
    if(data) {
        assert(length); *data=raw[r]; *length=scenario==1?32:2*(r?10:11);
        if(scenario==27 && r) { *length=32; raw[r][30]=1; }
        if(scenario==28 && r) *length=21;
        if(scenario==29 && r) *length=34;
        if(scenario==30 && r) *length=18;
        if(scenario==31 && r) *data=NULL;
        if(scenario==32 && r) *length=0;
        if(scenario==63 && r) raw[r][8]--;
    } else assert(prior_read && !length);
    if((scenario==20 && !r) || (scenario==21 && r)) return 0x30004;
    if(scenario==33 && r) return 0;
    if(scenario==34 && r) return 0x30000;
    return 0x30000+0x10*r;
}
static uint32_t adreno8_get(struct adreno8_block *b,const char *name,uint32_t off) {
    assert(!poison && b->phys==0x16520000 && b->size==0x10000 && off<b->size && !(off&3));
    assert(off!=ARC_RESPONSE || prior_read);
    b->base=0x100000; reads++;
    if((scenario==56 && !strcmp(name,"irq status after request")) ||
       (scenario==57 && off==ARC_COMMAND_STATUS) ||
       (((scenario==58 && triggers==1) || (scenario==59 && triggers==2)) &&
        !strcmp(name,"irq enable before restore"))) return UINT32_MAX;
    if((scenario==60 && !strcmp(name,"irq enable before request")) ||
       (scenario==61 && !strcmp(name,"irq enable before restore"))) regs[ARC_IRQ_ENABLE/4]|=1u<<5;
    return regs[off/4];
}
static void adreno8_put(struct adreno8_block *b,const char *name,uint32_t off,uint32_t value) {
    (void)name; assert(!poison && adreno8_reset_required && b->phys==0x16520000);
    assert(off==ARC_CONTROL || off==ARC_ENABLE || off==ARC_MSGID || off==ARC_ADDRESS ||
        off==ARC_DATA || off==ARC_WAIT || off==ARC_IRQ_ENABLE || off==ARC_IRQ_CLEAR);
    assert(!triggered || done || (off==ARC_CONTROL && value==(ARC_AMC_ENABLE|ARC_AMC_TRIGGER)));
    writes++;
    if(off==ARC_MSGID) assert(value==(prior_read?0x108u:0x10108u));
    if(off==ARC_ADDRESS) assert(value==0x30000 || value==0x30010);
    if(off==ARC_DATA) {
        assert(!prior_read && lookups==2);
        assert(value==(regs[ARC_ADDRESS/4]==0x30000?10u:9u)); voltage_writes++;
    }
    if(fail_at==writes) { regs[off/4]=value^1; return; }
    if(off==ARC_IRQ_CLEAR) {
        assert(done && value==ARC_TCS_BIT);
        if(scenario!=62) regs[ARC_IRQ_STATUS/4]&=~value;
        return;
    }
    if(off==ARC_CONTROL && value==(ARC_AMC_ENABLE|ARC_AMC_TRIGGER)) {
        assert(regs[ARC_CONTROL/4]==ARC_AMC_ENABLE && !(regs[ARC_IRQ_STATUS/4]&ARC_TCS_BIT));
        assert(regs[ARC_ENABLE/4]==1 && regs[ARC_WAIT/4]==unsigned(!prior_read));
        assert(lookups==2 || prior_read);
        rail=regs[ARC_ADDRESS/4]==0x30000?0:1;
        triggered=true; done=false; tx_polls=0; triggers++;
    }
    regs[off/4]=value;
}
static uint32_t adreno8_rd(vm_offset_t addr) {
    assert(addr==0x100000+ARC_IRQ_STATUS && triggered && !poison);
    reads++; polls++; tx_polls++;
    if((scenario==54 && triggers==1) || (scenario==55 && triggers==2)) { poison=true; return UINT32_MAX; }
    bool timeout=(scenario==50 && triggers==1) || (scenario==51 && triggers==2);
    if(tx_polls==3 && !timeout) {
        done=true;
        if(!prior_read) votes[rail]=regs[ARC_DATA/4];
        regs[ARC_IRQ_STATUS/4]|=ARC_TCS_BIT|4;
        regs[ARC_COMMAND_STATUS/4]=((scenario==52 && triggers==1) || (scenario==53 && triggers==2))?1u<<8:ARC_COMMAND_DONE;
        regs[ARC_STATUS/4]=1; regs[ARC_RESPONSE/4]=0;
    }
    return regs[ARC_IRQ_STATUS/4];
}
static void setup(void) {
    adreno8_gmu_from_pll=2;
    regs[0]=0x30800; regs[1]=0x04010100; regs[3]=0x800c8104;
    regs[ARC_CONTROL/4]=ARC_AMC_ENABLE;
    regs[0xd28/4]=regs[0xfc8/4]=regs[0x1268/4]=1;
    regs[ARC_IRQ_ENABLE/4]=scenario==2?3:1;
    for(unsigned i=3;i<8;i++) regs[(0xd00+i*0x2a0+0x28)/4]=1;
    const uint16_t values[]={0,16,64,128,192,256,320,384,416,448,480};
    for(unsigned r=0;r<2;r++) for(unsigned j=0;j<(r?10u:11u);j++) {
        raw[r][2*j]=values[j]&255; raw[r][2*j+1]=values[j]>>8;
    }
    switch(scenario) {
    case 9: adreno8_step=4; break;
    case 10: adreno8_newest_step=6; break;
    case 12: arc_state=ARC_CLOSED; break;
    case 13: adreno8_gmu_from_pll=3; break;
    case 14: adreno8_newest_step=17; break;
    case 15: adreno8_newest_step=16; break;
    case 16: adreno8_newest_step=18; break;
    case 17: adreno8_newest_step=0; break;
    case 18: adreno8_newest_step=UINT32_MAX; break;
    case 19: adreno8_newest_step=14; break;
    case 35: regs[0]=0x30700; break;
    case 36: regs[1]|=1u<<24; break;
    case 37: regs[0xd28/4]=0; break;
    case 38: regs[0xfc8/4]=0; break;
    case 39: regs[0x1268/4]=0; break;
    case 40: regs[ARC_ENABLE/4]=1; break;
    case 41: regs[ARC_WAIT/4]=1; break;
    case 42: regs[ARC_CONTROL/4]|=ARC_AMC_TRIGGER; break;
    case 43: regs[ARC_IRQ_STATUS/4]=ARC_TCS_BIT; break;
    case 44: regs[(0xd00+3*0x2a0+0x2c)/4]=1; break;
    case 45: regs[(0xd00+5*0x2a0+0x24)/4]=1; break;
    }
    if(scenario==11) {
        prior_read=true; assert(adreno8_rpmh_arc_read()); prior_read=false;
        reads=writes=lookups=triggers=polls=voltage_writes=0;
    }
    memcpy(before,regs,sizeof(regs));
}
int main(int argc,char **argv) {
    assert(argc>=2); scenario=atoi(argv[1]); if(argc>2)fail_at=atoi(argv[2]); setup();
    bool result=adreno8_rpmh_clock_prepare();
    if(fail_at) assert(!result && writes==fail_at && adreno8_reset_required);
    else assert(result==(scenario<=2 || scenario==14 || scenario==16 || scenario==18 || scenario==61));
    if((scenario>=3 && scenario<=13) || scenario==15 || scenario==17 || scenario==19) assert(!reads && !writes && !lookups);
    if((scenario>=20 && scenario<=45) || scenario==63) assert(!writes && !triggers);
    if(result) {
        assert(lookups==2 && triggers==2 && voltage_writes==2 && writes==34);
        assert(votes[0]==10 && votes[1]==9 && adreno8_reset_required);
        assert(regs[ARC_CONTROL/4]==ARC_AMC_ENABLE && !regs[ARC_ENABLE/4] && !regs[ARC_WAIT/4]);
        assert(regs[ARC_IRQ_STATUS/4]==4 && regs[ARC_IRQ_ENABLE/4]==((scenario==2?3u:1u)|(scenario==61?1u<<5:0)));
    }
    if(scenario>=50 && scenario<=55) {
        assert(triggers==(scenario%2+1) && voltage_writes==triggers);
        if(scenario<=51) assert(tx_polls==100000 && !done);
        if(scenario>=54) assert(tx_polls==1 && !done);
    }
    if((scenario>=56 && scenario<=59) || scenario==62) assert(triggers==(scenario==59?2u:1u));
    if(scenario==60) assert(!triggers && voltage_writes==1);
    for(unsigned i=0;i<0x10000/4;i++) {
        unsigned off=4*i;
        if(off!=ARC_IRQ_ENABLE && off!=ARC_IRQ_STATUS && !(off>=ARC_WAIT && off<=ARC_RESPONSE)) assert(regs[i]==before[i]);
    }
    unsigned r=reads,w=writes,l=lookups;
    assert(!adreno8_rpmh_clock_prepare());
    if(arc_state==ARC_CLOSED) assert(!adreno8_rpmh_arc_read());
    assert(reads==r && writes==w && lookups==l);
}
'''


def run(source, target, cases, reject=False, model=MODEL):
    fixture = runpy.run_path(str(ROOT / "tools/test-adreno8-arc-read.py"))
    (target / "adreno8_private.h").write_text(fixture["HEADER"])
    (target / "adreno8_rpmh_private.h").write_text((DEV / "adreno8_rpmh_private.h").read_text())
    (target / "adreno8_rpmh.c").write_text(source)
    (target / "check.cpp").write_text(model)
    binary = target / "check"
    subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                    "-Wno-missing-field-initializers", "-fsanitize=address,undefined",
                    str(target / "check.cpp"), "-o", str(binary)], check=True)
    for args in cases:
        result = subprocess.run([str(binary), *map(str, args)], capture_output=True, text=True)
        if reject:
            assert result.returncode, f"mutation survived {args}"
        elif result.returncode:
            raise RuntimeError(f"case {args}:\n" + result.stdout + result.stderr)


def main():
    production = (DEV / "adreno8_rpmh.c").read_text()
    cases = [(n,) for n in range(64) if n not in (39, 54, 56)]
    cases += [(0, n) for n in range(1, 91) if n not in (8, 12, 22, 26, 38, 43, 53, 57, 69, 74, 84, 88)]
    mutations = [
        ("wrong_slot_offset", "#define ARC_MSGID 0xfd4u", "#define ARC_MSGID 0x1274u", (0,)),
        ("wrong_slot_irq", "#define ARC_TCS_BIT (1u << 1)", "#define ARC_TCS_BIT (1u << 2)", (0,)),
        ("drop_inherited", "if (required[i] < votes[i].selector) required[i] = votes[i].selector;", "", (31,)),
        ("wrong_selector_units", "votes[i].selector = required[i];", "votes[i].selector = 128;", (0,)),
        ("skip_write_ack", "if (!arc_transfer(&votes[i], true) ||", "if (false ||", (0,)),
        ("skip_verify", "votes[i].selector != required[i]) return false;", "false) return false;", (47,)),
        ("allow_solver", "(solver & (1u << 24)) == 0", "true", (11,)),
        ("skip_sleep_guard", 'i = 3; i < 8', 'i = 8; i < 8', (13,)),
        ("ignore_touch", 'zero != 0', 'false', (4,)),
        ("repeat", "adreno8_gmu_from_pll > 2 || arc_state == ARC_CLOSED", "adreno8_gmu_from_pll > 2", (0,)),
        ("irq_peer_loss", "irq & ~(ARC_TCS_BIT & ~irq_before)", "irq_before", (49,)),
        ("write_read_message", "write ? 0x10108u : 0x108u", "0x108u", (0,)),
        ("timeout_cleanup", 'printf("adreno8: ARC transaction completion unconfirmed;',
         'arc_write("premature cleanup", ARC_ENABLE, 0); printf("adreno8: ARC transaction completion unconfirmed;', (42,)),
        ("poll_unreadable", 'if (irq == 0xffffffffu) {', 'if (false) {', (62,)),
        ("command_unreadable", 'command == 0xffffffffu ||', '', (61,)),
        ("restore_unreadable", 'if (irq == 0xffffffffu ||\n\t    !arc_write("irq restore TCS1 bit",',
         'if (!arc_write("irq restore TCS1 bit",', (63,)),
    ]
    with tempfile.TemporaryDirectory(prefix="adreno8-rpmh-clock-") as directory:
        target = Path(directory)
        run(production, target, cases)
        for name, old, new, case in mutations:
            assert production.count(old) == 1, name
            run(production.replace(old, new, 1), target, [case], True)
        highest_cases = [(n,) for n in (*range(46), *range(50,64))]
        highest_cases += [(0,n) for n in range(1,35) if n not in (10,15,27,32)]
        run(production, target, highest_cases, model=HIGHEST_MODEL)
        highest_mutations = [
            ("highest_stage", "adreno8_step != 5 ||", "", (9,)),
            ("highest_newest", "(adreno8_newest_step != 5 && adreno8_newest_step < 17) ||", "", (10,)),
            ("highest_reject_17", "(adreno8_newest_step != 5 && adreno8_newest_step < 17)", "adreno8_newest_step != 5", (14,)),
            ("highest_newest_range", "(adreno8_newest_step != 5 && adreno8_newest_step < 17)", "(adreno8_newest_step < 5 || adreno8_newest_step > 17)", (15,)),
            ("highest_prior_read", "|| read_used", "|| (read_used && false)", (11,)),
            ("highest_map", "memcmp(votes[i].levels, expected, count * sizeof(expected[0])) != 0", "false", (24,)),
            ("highest_padding", "level != (j < count ? expected[j] : 0)", "false && level != (j < count ? expected[j] : 0)", (27,)),
            ("highest_length", "length > 32 ||", "", (29,)),
            ("highest_selector", "votes[i].selector = count - 1;", "votes[i].selector = 3;", (0,)),
            ("highest_missing_ack", "if (!arc_transfer(&votes[i], true)) return false;", "if (false) return false;", (0,)),
            ("highest_arc_read", "if (!arc_transfer(&votes[i], true)) return false;", "if (!arc_transfer(&votes[i], false)) return false;", (0,)),
        ]
        for name, old, new, case in highest_mutations:
            assert production.count(old) == 1, name
            run(production.replace(old, new, 1), target, [case], True, HIGHEST_MODEL)
        unreadable_irq = production.replace('irq != 0xffffffffu && (irq & ARC_TCS_BIT)', '(irq & ARC_TCS_BIT)', 1)
        unreadable_irq = unreadable_irq.replace('irq == 0xffffffffu || !(irq & ARC_TCS_BIT)', '!(irq & ARC_TCS_BIT)', 1)
        assert unreadable_irq != production
        run(unreadable_irq, target, [(60,)], True)
    print(f"ARC highest stage 5, targets 5/17: {len(highest_cases)} production scenarios PASS; {len(highest_mutations)} mutations rejected")
    print(f"ARC ACTIVE CX/MX: {len(cases)} production scenarios PASS; {len(mutations) + 1} mutations rejected")


if __name__ == "__main__":
    main()
