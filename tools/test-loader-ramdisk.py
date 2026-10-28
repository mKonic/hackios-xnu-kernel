#!/usr/bin/env python3
"""Exercise the production RAM-upload state and download branches without USB hardware.

The C harness uses the real choose_window, command dispatch through download, and
command-loop interruption path. EFI allocation and USB transfers are host stubs.
The host-script tests execute stage-system.sh's Python with a virtual large file.
"""
import contextlib
import io
import os
from pathlib import Path
import re
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
USB_SOURCE = (ROOT / 'xnuloader/loader_06_usb_entry.inc').read_text()
BASE_SOURCE = (ROOT / 'xnuloader/loader_01_base_log_dt.inc').read_text()
LAYOUT_SOURCE = (ROOT / 'xnuloader/loader_05_layout_run_kernel.inc').read_text()


def function(source, name):
    match = re.search(r'static[^;{}]*\n' + name + r'\([^;]*?\)\n\{', source)
    assert match, name
    start = match.start()
    level = 1
    pos = match.end()
    while level:
        level += (source[pos] == '{') - (source[pos] == '}')
        pos += 1
    return source[start:pos] + '\n'


PRELUDE = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdarg.h>
#include <setjmp.h>
static unsigned checks;
#define CK(x) do { checks++; if (!(x)) { fprintf(stderr, "line %d: %s\n", __LINE__, #x); return 1; } } while (0)
static uint8_t payload[4096], target[256], receive_buffer[1024 * 1024];
#define PAYLOAD_BASE ((uint64_t)(uintptr_t)payload)
#define PAYLOAD_RESERVE_SIZE 0x4000000ULL
#define PAYLOAD_CONTAINER_MAX 0x3700000ULL
#define USB_CHUNK (1024 * 1024)
struct payload_header { uint32_t magic, size; };
struct payload_info { uint64_t max_mem, forced_base; };
typedef uint64_t efi_status;
typedef struct { uint32_t type; uint64_t phys_start, pages; } efi_memory_descriptor;
#define EfiConventionalMemory 7
#define EfiLoaderData 2
#define AllocateAddress 2
#define EFI_ERROR(x) ((x) != 0)
static uint64_t free_base = 0x8c0000000ULL, free_size = 0x340000000ULL;
static unsigned allocations;
static efi_status allocation_error;
static efi_status get_map(uint64_t *size, void *buffer, uint64_t *key, uint64_t *stride, uint32_t *version)
{
    (void)key; (void)version;
    efi_memory_descriptor d = { 7, free_base, free_size >> 12 };
    memcpy(buffer, &d, sizeof(d)); *size = *stride = sizeof(d); return 0;
}
static efi_status allocate(int policy, int type, uint64_t pages, uint64_t *address)
{ (void)policy; (void)type; (void)pages; (void)address; allocations++; return allocation_error; }
static struct { efi_status (*get_memory_map)(uint64_t *, void *, uint64_t *, uint64_t *, uint32_t *);
    efi_status (*allocate_pages)(int, int, uint64_t, uint64_t *); } services = { get_map, allocate }, *BS = &services;
static uint64_t win_base, win_size, resident_rd_size, payload_bytes;
static int download_to_ramdisk, kernel_resources_held, firmware_fault, usb_connected = 1;
static int usb_out_lost, usb_in_lost, screen_quiet;
static int usb_out_owned = 71, usb_in_owned = 72;
struct kernel_live { int stop; };
static struct kernel_live *running_live;
static struct { uint64_t base; uint32_t width, height, stride; } SCREEN;
static uint32_t log_len;
static char log_buf[512], last_result[64] = "idle", answer[80];
static uint8_t *usb_rx_buf = receive_buffer;
static unsigned rx_calls, data_replies;
static uint64_t rx_left, rx_position;
static jmp_buf loop_escape;
static int escape_loop;
static void print(const char *format, ...) { (void)format; }
static int str_eq(const char *a, const char *b) { return !strcmp(a, b); }
static uint64_t parse_number(const char *p, int base) { uint64_t n = 0; (void)base; sscanf(p, "%llx", (unsigned long long *)&n); return n; }
static void fb_reply(const char *status, const char *message)
{ snprintf(answer, 64, "%s%s", status, message); }
static void fb_reply_data(uint64_t size) { assert(size && size <= 0xffffffffULL); data_replies++; }
static int fb_getvar_identity(const char *var) { (void)var; return 0; }
static int parse_payload(struct payload_info *pi, int verbose) { (void)verbose; memset(pi, 0, sizeof(*pi)); return 1; }
enum { LC_STOP_HELD, LC_STOP_NOTIFY, LC_STOP_IDLE };
static int lc_stop_action(int hold, int live) { return live ? LC_STOP_NOTIFY : hold ? LC_STOP_HELD : LC_STOP_IDLE; }
static int64_t usb_wait(uint8_t endpoint, int deliver)
{ (void)endpoint; (void)deliver; assert(escape_loop); longjmp(loop_escape, 1); }
static int64_t usb_rx(uint8_t *buffer, uint64_t length, int command)
{
    if (command && escape_loop) longjmp(loop_escape, 1);
    rx_calls++;
    if (!rx_left) return -1;
    if (length > rx_left) length = rx_left;
    for (uint64_t i = 0; i < length; i++) buffer[i] = (uint8_t)(rx_position++ + 17);
    rx_left -= length;
    return (int64_t)length;
}
'''

SCENARIOS = r'''
static void clean(void)
{
    ramdisk_forget(); win_base = win_size = resident_rd_size = payload_bytes = 0;
    download_to_ramdisk = kernel_resources_held = firmware_fault = usb_out_lost = usb_in_lost = 0;
    running_live = NULL; usb_connected = 1; rx_calls = data_replies = allocations = 0;
    allocation_error = 0; free_base = 0x8c0000000ULL; free_size = 0x340000000ULL;
    answer[0] = 0;
}
static void backing(uint64_t window)
{
    win_base = (uint64_t)(uintptr_t)target - RESIDENT_RD_OFFSET; win_size = window;
    memset(target, 0xa5, sizeof(target)); rx_position = 0;
}
int main(void)
{
    uint64_t length;
    clean(); fb_command("getvar:ramdisk-protocol"); CK(!strcmp(answer, "OKAY1") && allocations == 0);
    fb_command("oem ramdisk begin 140000000 2c0000000");
    CK(!strcmp(answer, "OKAY") && win_size == 0x2c0000000ULL && allocations == 1);
    CK(ramdisk_pending() && resident_rd_size == 0);
    fb_command("getvar:ramdisk-state"); CK(!strcmp(answer, "OKAYreceiving 0 140000000 2c0000000"));
    fb_command("oem run"); CK(!strncmp(answer, "FAIL", 4));
    fb_command("oem selftest"); CK(!strncmp(answer, "FAIL", 4));
    fb_command("oem ramdisk"); CK(!strncmp(answer, "FAIL", 4));
    CK(ramdisk_block_size("ffffffff", &length) && length == 0xffffffffULL);
    ramdisk_block_done(length, length); CK(resident_rd_size == 0 && ramdisk_pending());
    CK(ramdisk_block_size("40000001", &length)); ramdisk_block_done(length, length);
    CK(resident_rd_size == 0x140000000ULL && !ramdisk_pending());
    CK(!ramdisk_command("oem run"));
    fb_command("getvar:ramdisk-state"); CK(!strcmp(answer, "OKAYready 140000000 140000000 2c0000000"));

    const char *bad[] = { "0 2c0000000", "1 0", "1 2c0000001", "1 400000000",
        "10000000000000000 2c0000000", "ffffffffffffffff 2c0000000", "1 10000000000000000",
        "1", "1 2c0000000 trailing", "1x 2c0000000", "1  2c0000000", "288000001 2c0000000" };
    for (unsigned i = 0; i < sizeof(bad)/sizeof(bad[0]); i++) {
        CK(ramdisk_begin(bad[i]) != NULL); CK(resident_rd_size == 0x140000000ULL);
    }
    CK(ramdisk_begin("8 180000000") != NULL && allocations == 1);
    CK(!ramdisk_begin("288000000 2c0000000") && ramdisk_pending() && !resident_rd_size);
    clean(); free_size = 0x180000000ULL;
    CK(ramdisk_begin("140000000 2c0000000") != NULL && resident_rd_size == 0);
    clean(); allocation_error = 1; CK(ramdisk_begin("8 2c0000000") != NULL && win_size == 0);
    clean(); win_base = ~0ULL - 15; win_size = 0x2c0000000ULL;
    CK(ramdisk_begin("8 2c0000000") != NULL);

    clean(); backing(0x2c0000000ULL);
    CK(!ramdisk_begin("8 2c0000000")); rx_left = 4; fb_command("download:4");
    CK(resident_rd_size == 0 && ramdisk_transfer.received == 4 && target[3] == 20 && target[4] == 0xa5);
    CK(!ramdisk_begin("8 2c0000000") && ramdisk_transfer.received == 0);
    rx_left = 4; rx_position = 0; fb_command("download:4");
    rx_left = 2; fb_command("download:4");
    CK(!strncmp(answer, "FAIL", 4) && ramdisk_transfer.failed && resident_rd_size == 0);
    CK(ramdisk_transfer.received == 4 && target[5] == 22 && target[6] == 0xa5);
    unsigned calls = rx_calls; rx_left = 4; fb_command("download:4"); CK(rx_calls == calls && !strncmp(answer, "FAIL", 4));
    fb_command("oem run"); CK(!strncmp(answer, "FAIL", 4));
    fb_command("oem ramdisk none"); CK(!ramdisk_pending() && !resident_rd_size && !download_to_ramdisk);
    CK(!ramdisk_begin("8 2c0000000")); rx_left = 4; rx_position = 0; fb_command("download:4");
    rx_left = 4; fb_command("download:4");
    CK(!strcmp(answer, "OKAY") && resident_rd_size == 8 && target[0] == 17 && target[7] == 24 && target[8] == 0xa5);
    CK(!ramdisk_begin("8 2c0000000")); fb_command("download:100000000"); CK(ramdisk_transfer.failed && !resident_rd_size);
    CK(!ramdisk_begin("8 2c0000000")); fb_command("download:10000000000000008"); CK(ramdisk_transfer.failed);
    CK(!ramdisk_begin("8 2c0000000")); fb_command("download:9"); CK(ramdisk_transfer.failed);

    const char *commands[] = { "oem ramdisk begin 8 2c0000000", "oem ramdisk none", "download:4", "oem run" };
    for (unsigned mode = 0; mode < 3; mode++) {
        clean(); backing(0x2c0000000ULL); CK(!ramdisk_begin("8 2c0000000"));
        struct kernel_live live = {0};
        kernel_resources_held = mode == 0; firmware_fault = mode == 1; running_live = mode == 2 ? &live : NULL;
        for (unsigned i = 0; i < sizeof(commands)/sizeof(commands[0]); i++) {
            fb_command((char *)commands[i]); CK(!strncmp(answer, "FAIL", 4));
            CK(ramdisk_transfer.total == 8 && ramdisk_transfer.received == 0 && rx_calls == 0);
        }
        fb_command("getvar:ramdisk-state"); CK(!strncmp(answer, "OKAY", 4));
    }
    for (unsigned mode = 0; mode < 4; mode++) {
        clean(); backing(0x2c0000000ULL); CK(!ramdisk_begin("8 2c0000000"));
        usb_connected = mode != 0; firmware_fault = mode == 1; usb_out_lost = mode == 2; usb_in_lost = mode == 3;
        escape_loop = 1; if (!setjmp(loop_escape)) usb_command_loop(); escape_loop = 0;
        CK(ramdisk_transfer.failed && resident_rd_size == 0);
        CK(usb_out_owned == 71 && usb_in_owned == 72);
    }
    clean(); backing(MAX_MEM_SIZE); fb_command("oem ramdisk"); CK(download_to_ramdisk && !strcmp(answer, "OKAY"));
    rx_left = 8; fb_command("download:8"); CK(resident_rd_size == 8 && ramdisk_transfer.total == 0 && target[7] == 24);
    fb_command("oem ramdisk"); rx_left = 2; fb_command("download:8"); CK(!resident_rd_size && !strncmp(answer, "FAIL", 4));
    fb_command("oem ramdisk"); fb_command("download:100000000"); CK(!strncmp(answer, "FAIL", 4));
    fb_command("oem ramdisk none"); CK(!download_to_ramdisk && !resident_rd_size);
    printf("loader ramdisk C checks: %u passed\n", checks);
    return 0;
}
'''


def test_loader():
    definitions = '\n'.join(re.search(r'^#define ' + key + r'\s+\S+', BASE_SOURCE, re.M).group()
                            for key in ('BASE_ALIGN', 'MAX_MEM_SIZE', 'MAX_MEM_SIZE_LIMIT',
                                        'RESIDENT_RD_OFFSET', 'RESIDENT_RD_RESERVE'))
    command = function(USB_SOURCE, 'fb_command')
    command = command[:command.index('\t\t} else if (starts_with(cmd, "upload"))')] + '\t\t}\n\t}\n}\n'
    assert command.index('lc_cmd_denied(') < command.index('ramdisk_command(cmd)')
    source = PRELUDE + definitions + '\n' + function(LAYOUT_SOURCE, 'choose_window')
    for name in ('lc_starts', 'lc_blocked_cmd', 'lc_busy', 'lc_cmd_denied'):
        source += function(BASE_SOURCE, name)
    source += function(USB_SOURCE, 'starts_with')
    source += (ROOT / 'xnuloader/loader_ramdisk.inc').read_text()
    source += command + function(USB_SOURCE, 'usb_command_loop') + SCENARIOS
    with tempfile.TemporaryDirectory(prefix='hackios-ramdisk-test-') as directory:
        cfile = Path(directory) / 'test.c'
        binary = Path(directory) / 'test'
        cfile.write_text(source)
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', str(cfile), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
        mutations = {
            'partial publication': ('if (ramdisk_transfer.received == ramdisk_transfer.total) {',
                                    'if (ramdisk_transfer.received != 0) {'),
            'short block accepted': ('ramdisk_transfer.failed || received != wanted ||',
                                     'ramdisk_transfer.failed ||'),
            'failed session receives data': ('ramdisk_transfer.failed || !ramdisk_hex(&argument, size)',
                                              '!ramdisk_hex(&argument, size)'),
            'missing run refusal': ('ramdisk_pending() && (starts_with(cmd, "oem run")',
                                    '0 && (starts_with(cmd, "oem run")'),
        }
        for name, (before, after) in mutations.items():
            assert source.count(before) == 1, name
            cfile.write_text(source.replace(before, after))
            subprocess.run(['cc', '-std=c11', str(cfile), '-o', str(binary)], check=True)
            result = subprocess.run([str(binary)], capture_output=True)
            assert result.returncode, name
            print('mutation rejected:', name)


HOST_CODE = (ROOT / 'tools/stage-system.sh').read_text().split("<<'PYTHON'\n", 1)[1].rsplit('\nPYTHON', 1)[0]


def test_host(total, *, capability='1', fail_block=0, wrong_progress=False, window=True, held=False,
              short_source=False, changed_source=False):
    state = SimpleNamespace(total=0, received=0, window=0, length=0, position=0, blocks=0, closed=False,
                            cancelled=False, stat_calls=0)
    calls = []

    class VirtualFile:
        def __init__(self, mode):
            self.mode = mode
            if mode == 'wb':
                state.length = 0

        def __enter__(self): return self
        def __exit__(self, *args): return None
        def fileno(self): return 123
        def read(self, count):
            count = min(count, total - int(short_source) - state.position)
            state.position += count
            return b'x' * count
        def write(self, data): state.length += len(data)

    class VirtualPath:
        def __init__(self, value): self.value = str(value)
        def __str__(self): return self.value
        def __truediv__(self, name): return VirtualPath(self.value + '/' + name)
        def is_file(self): return True
        def stat(self): return SimpleNamespace(st_size=total, st_mtime_ns=1)
        def open(self, mode): return VirtualFile(mode)

    class Temporary:
        def __init__(self, **kwargs): pass
        def __enter__(self): return '/virtual/blocks'
        def __exit__(self, *args): state.closed = True

    def run(command, **kwargs):
        calls.append(command)
        args = command[3:] if command[1:3] == ['-s', 'xnuloader'] else command[1:]
        output = ''
        code = 0
        if args == ['devices']: output = 'xnuloader\tfastboot\n'
        elif args == ['getvar', 'product']: output = 'product: xnuloader\n'
        elif args == ['getvar', 'result']: output = 'result: hung-held\n' if held else 'result: idle\n'
        elif args == ['getvar', 'ramdisk-protocol']: output = 'ramdisk-protocol: ' + capability + '\n'
        elif args[:3] == ['oem', 'ramdisk', 'begin']:
            state.total, state.window = (int(v, 16) for v in args[3:])
        elif args == ['oem', 'ramdisk', 'none']: state.cancelled = True
        elif args == ['getvar', 'ramdisk-state']:
            phase = 'ready' if state.received == state.total else 'receiving'
            reported = state.received + (1 if wrong_progress and state.blocks else 0)
            output = f'(bootloader) ramdisk-state: {phase} {reported:x} {state.total:x} {state.window:x}\n'
        elif args[0] == 'stage' and total > 0xffffffff:
            state.blocks += 1
            assert 0 < state.length <= 256 * 1024 * 1024
            if state.blocks == fail_block: code = 1; output = 'FAILED transfer\n'
            else: state.received += state.length
        elif args not in (['oem', 'ramdisk'], ['stage', '/virtual/system.apfs']):
            raise AssertionError(args)
        return SimpleNamespace(returncode=code, stdout=output)

    env = {'STAGE_SYSTEM_IMAGE': '/virtual/system.apfs', 'STAGE_SYSTEM_FASTBOOT': 'fake-fastboot'}
    if window: env['STAGE_SYSTEM_WINDOW'] = '2c0000000'
    status = 0
    output = io.StringIO()
    def fstat(descriptor):
        assert descriptor == 123
        state.stat_calls += 1
        return SimpleNamespace(st_size=total, st_mtime_ns=2 if changed_source and state.stat_calls > 1 else 1)
    with patch.dict(os.environ, env, clear=True), patch('pathlib.Path', VirtualPath), \
            patch('subprocess.run', run), patch('tempfile.TemporaryDirectory', Temporary), \
            patch('os.fstat', fstat), \
            contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            exec(compile(HOST_CODE, 'stage-system.sh', 'exec'), {})
        except SystemExit as error:
            status = error.code
    return status, state, calls


def main():
    test_loader()
    status, state, calls = test_host(1024, capability='', window=False)
    assert status == 0 and not any('ramdisk-protocol' in c or 'ramdisk-state' in c for c in calls)
    assert calls[-2:] == [['fake-fastboot', '-s', 'xnuloader', 'oem', 'ramdisk'],
                         ['fake-fastboot', '-s', 'xnuloader', 'stage', '/virtual/system.apfs']]
    status, state, calls = test_host(0x100000001)
    assert status == 0 and state.received == 0x100000001 and state.blocks == 17 and state.closed
    status, state, calls = test_host(0x100000001, window=False)
    assert status == 1 and not calls
    status, state, calls = test_host(0x100000001, capability='')
    assert status == 1 and state.total == 0
    status, state, calls = test_host(0x100000001, fail_block=2)
    assert status == 1 and state.blocks == 2 and state.closed
    status, state, calls = test_host(0x100000001, wrong_progress=True)
    assert status == 1 and state.blocks == 1 and state.closed
    status, state, calls = test_host(1024, held=True)
    assert status == 1 and not any('stage' in c or 'oem' in c for c in calls)
    status, state, calls = test_host(0x100000001, short_source=True)
    assert status == 1 and state.blocks == 16 and state.closed
    status, state, calls = test_host(0x100000001, changed_source=True)
    assert status == 1 and state.cancelled and state.closed
    print('stage-system: nine cases passed (legacy, large, window, capability, transfer, progress, held, source changes)')


if __name__ == '__main__':
    main()
