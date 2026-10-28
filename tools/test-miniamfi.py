#!/usr/bin/env python3
"""Exercise MiniAMFI's syscall ABI with bounded user-memory copies on the host."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "xnu/files/bsd/kern/kern_minimac.c"
PREAMBLE = r'''
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#ifndef __unused
#define __unused __attribute__((unused))
#endif
#define ENOPOLICY 103
#define MIN(a, b) ((a) < (b) ? (a) : (b))
typedef uint64_t user_addr_t;
struct proc { int unused; };
enum { BASE = 0x1000, ARG = 0x1100, DATA = 0x2000, OUT = 0x7000 };
static unsigned char memory[0x8000];
static uint64_t read_limit, write_limit;
static unsigned output_calls, fail_output_call, constraint_bytes;
static int copyin(user_addr_t address, void *dest, size_t count)
{
    if (address < BASE || address > read_limit || count > read_limit - address)
        return EFAULT;
    if (address >= DATA && address < DATA + 0x4000) {
        assert(count <= 256);
        constraint_bytes += count;
    }
    memcpy(dest, memory + address - BASE, count);
    return 0;
}
static int copyout(const void *src, user_addr_t address, size_t count)
{
    ++output_calls;
    if (output_calls == fail_output_call) return EFAULT;
    if (address < BASE || address > write_limit || count > write_limit - address)
        return EFAULT;
    memcpy(memory + address - BASE, src, count);
    return 0;
}
'''
TEST = r'''
static void put(unsigned address, uint64_t value, unsigned size)
{
    memcpy(memory + address - BASE, &value, size);
}
static uint32_t code(void)
{
    uint32_t value;
    memcpy(&value, memory + OUT - BASE, sizeof(value));
    return value;
}
static void reset(uint32_t length)
{
    memset(memory, 0xa5, sizeof(memory));
    read_limit = write_limit = BASE + sizeof(memory);
    output_calls = fail_output_call = constraint_bytes = 0;
    /* Wire positions are independent of the production C structure. */
    put(ARG + 0x20, DATA, 8);
    put(ARG + 0x28, length, 4);
    put(ARG + 0x2c, 0xaaaaaaaa, 4);
    put(ARG + 0x30, OUT, 8);
    put(ARG + 0x38, 0x404, 4);
    put(ARG + 0x3c, 0xbbbbbbbb, 4);
}
static int run(void) { return miniamfi_policy_syscall(NULL, 0x66, ARG); }
static void check_output(unsigned length, unsigned char value)
{
    for (unsigned i = 0; i < length; ++i)
        assert(memory[OUT - BASE + i] == value);
    assert(memory[OUT - BASE - 1] == 0xa5);
    assert(memory[OUT - BASE + length] == 0xa5);
}
int main(void)
{
    const unsigned lengths[] = {1, 255, 256, 257, 0x4000};
    for (unsigned i = 0; i < sizeof(lengths) / sizeof(lengths[0]); ++i) {
        reset(lengths[i]);
        assert(run() == 0 && code() == 0);
        assert(constraint_bytes == lengths[i]);
        check_output(0x404, 0);
    }
    const unsigned invalid_lengths[] = {0, 0x4001, UINT32_MAX};
    for (unsigned i = 0; i < sizeof(invalid_lengths) / sizeof(invalid_lengths[0]); ++i) {
        reset(invalid_lengths[i]);
        assert(run() == 0 && code() == 5 && constraint_bytes == 0);
        for (unsigned j = 4; j < 0x404; ++j) assert(memory[OUT - BASE + j] == 0);
    }
    reset(1); put(ARG + 0x20, 0, 8);
    assert(run() == 0 && code() == 5);
    reset(1); put(ARG + 0x30, 0, 8);
    assert(run() == EFAULT && output_calls == 0);
    reset(1); put(ARG + 0x20, UINT64_MAX - 1, 8); put(ARG + 0x28, 4, 4);
    assert(run() == EFAULT && code() == 5 && constraint_bytes == 0);

    const unsigned output_sizes[] = {0, 1, 3, 4, 0x403, 0x405, UINT32_MAX};
    for (unsigned i = 0; i < sizeof(output_sizes) / sizeof(output_sizes[0]); ++i) {
        reset(1); put(ARG + 0x38, output_sizes[i], 4);
        if (output_sizes[i] < 4) {
            assert(run() == EINVAL && output_calls == 0);
            check_output(0x404, 0xa5);
        } else {
            assert(run() == 0 && code() == 5);
            assert(memory[OUT - BASE + 4] == 0xa5);
        }
        assert(constraint_bytes == 0);
    }
    reset(300); read_limit = DATA + 299;
    assert(run() == EFAULT && code() == 5 && constraint_bytes == 256);
    reset(1); read_limit = DATA;
    assert(run() == EFAULT && code() == 5 && constraint_bytes == 0);
    reset(1); read_limit = ARG + 0x3f;
    assert(run() == EFAULT && output_calls == 0);
    reset(1); assert(miniamfi_policy_syscall(NULL, 0x66, 0) == EFAULT);
    reset(1); write_limit = OUT;
    assert(run() == EFAULT && constraint_bytes == 0);
    reset(1); write_limit = OUT + 0x403;
    assert(run() == EFAULT && code() == 5 && constraint_bytes == 0);
    reset(1); fail_output_call = 3;
    assert(run() == EFAULT && code() == 5 && constraint_bytes == 1);

    /* Existing policy requests must keep their previous wire contracts. */
    reset(1);
    assert(miniamfi_policy_syscall(NULL, 0x60, OUT) == 0);
    uint64_t value;
    memcpy(&value, memory + OUT - BASE, 8);
    assert(value == 0x86 && memory[OUT - BASE + 8] == 0xa5);
    reset(1); put(ARG + 8, OUT, 8);
    assert(miniamfi_policy_syscall(NULL, 0x5a, ARG) == 0);
    memcpy(&value, memory + OUT - BASE, 8);
    assert(value == 0x1ff && memory[OUT - BASE + 8] == 0xa5);
    reset(1);
    assert(miniamfi_policy_syscall(NULL, 0x67, ARG) == ENOPOLICY);
    assert(output_calls == 0 && constraint_bytes == 0);
    puts("PASS: AMFI 0x66 ABI, result/errno separation, bounded copies, existing requests");
}
'''


def main():
    source = SOURCE.read_text()
    begin = source.index("#define AMFI_SYSCALL_CHECK_DYLD_POLICY")
    end = source.index("static const struct mac_policy_ops miniamfi_ops", begin)
    with tempfile.TemporaryDirectory(prefix="miniamfi-test-") as folder:
        folder = Path(folder)
        test = folder / "test.c"
        binary = folder / "test"
        test.write_text(PREAMBLE + source[begin:end] + TEST)
        subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror", "-g",
                        "-fsanitize=address,undefined", str(test), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    main()
