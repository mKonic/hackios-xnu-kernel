#!/usr/bin/env python3
"""Put names on the user-space addresses the kernel's bring-up aids print.

usage: symstack.py LOG [PROCESS]

Reads the "ustacks:" (boot-args ustacks=NAME ustacks_at=SECONDS [ustacks_ms=PERIOD]), "usertrap:" and
"userabort:" lines of a console log (and, before a "userabort:", the "First throw call stack" of the exception
that led to it), subtracts the shared cache slide and looks every address up in the table of the shared
cache's functions (ios165/dsc-functions.txt, which qemu/dsc_symbols.py makes). Addresses outside the cache
(the program itself) are shown as they are.
"""
import os
import re
import sys

import dsc_symbols

CACHE_LOW, CACHE_HIGH = dsc_symbols.CACHE_LOW, dsc_symbols.CACHE_HIGH
names = dsc_symbols.Names()
collecting = True                       # the first pass only notes the addresses, to look them up together


def name(address, slide):
    unslid = address - slide
    if not CACHE_LOW <= unslid < CACHE_HIGH:
        return f'{address:#x} (not in the shared cache)'
    if collecting:
        names.want(unslid)
        return ''
    return names.describe(unslid)


def main():
    log = sys.argv[1]
    only = sys.argv[2] if len(sys.argv) > 2 else None
    slide, show = 0, True
    thrown = None                       # the addresses after "*** First throw call stack:", until the abort
    for line in open(log, errors='replace'):
        line = line.rstrip('\n')
        if re.fullmatch(r'\((0x[0-9a-f]+ ?)+\)', line):
            thrown = line[1:-1].split()
            continue
        m = re.search(r'userabort: (\S+)\[(\d+)\] aborts, shared cache slide (0x[0-9a-f]+); called from: (.*)', line)
        if m:
            if only is None or m.group(1) == only:
                print(f'userabort: {m.group(1)}[{m.group(2)}]')
                for text in m.group(4).split():
                    print(f'    {name(int(text, 16), int(m.group(3), 16))}')
                if thrown:
                    print('  the exception was thrown from')
                    for text in thrown:
                        print(f'    {name(int(text, 16), int(m.group(3), 16))}')
            thrown = None
            continue
        m = re.search(r'(ustacks|usertrap): (\S+)\[(\d+)\].*shared cache slide (0x[0-9a-f]+)', line)
        if m:
            slide = int(m.group(4), 16)
            show = only is None or m.group(2) == only
            if show:
                print(line[line.index(m.group(1)):])
            continue
        if not show:
            continue
        m = re.search(r'ustacks:  thread (0x[0-9a-f]+)([^:]*): (.*)', line)
        if m:
            print(f'  thread {m.group(1)}{m.group(2)}')
            for depth, text in enumerate(m.group(3).split()):
                if depth == 1:
                    continue            # the link register: usually stale, the frame chain has the caller
                print(f'    {name(int(text, 16), slide)}')
            continue
        m = re.search(r'usertrap:   called from (0x[0-9a-f]+)', line)
        if m:
            print(f'    {name(int(m.group(1), 16), slide)}')
        elif 'usertrap:   x' in line:
            print('  ' + line[line.index('usertrap:') + 10:])


if __name__ == '__main__':
    with open(os.devnull, 'w') as quiet:
        stdout, sys.stdout = sys.stdout, quiet
        main()
        sys.stdout = stdout
    names.resolve()
    collecting = False
    main()
