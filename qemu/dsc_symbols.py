#!/usr/bin/env python3
"""Make a table of every function in the iOS dyld shared cache, for naming addresses in bulk.

usage: dsc_symbols.py

Writes ios165/dsc-functions.txt: one line per function, "ADDRESS END IMAGE NAME", sorted, unslid (END: where
it ends, as far as the function starts tell; what lies between a function's end and the next one is stubs --
branch islands, Objective-C's objc_msgSend stubs -- or data). The names
are what the cache still has: exported symbols, Objective-C methods ("-[CALayer display]"), and for the
rest "func_ADDRESS" -- the IPSW has no local symbols (they ship in a separate .symbols file that only
Apple's device support carries), so QuartzCore's C++ inside (CA::OGL::...) is nameless. ipsw insists on
that file, so a stand-in that says "no local symbols" is made next to the cache first.
qemu/profile.py and qemu/symstack.py read the table.
"""
import bisect
import os
import re
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(HERE, 'ios165')
CACHE = os.path.join(BASE, '20F66__iPhone10,3_6/dyld_shared_cache_arm64')
TABLE = os.path.join(BASE, 'dsc-functions.txt')
IPSW = os.path.join(HERE, 'xnu-deps/host/bin/ipsw')
KERNEL = os.path.join(HERE, 'xnu-src/xnu-8796.121.2/BUILD/obj/DEVELOPMENT_ARM64_BCM2837/mach.development.bcm2837')
CACHE_LOW, CACHE_HIGH = 0x180000000, 0x280000000        # unslid addresses of the shared cache
KERNEL_LOW = 0xfffffe0000000000


def make_standin():
    """A .symbols file that is the cache's header with no mappings, images or local symbols."""
    path = CACHE + '.symbols'
    if os.path.exists(path):
        return
    h = bytearray(open(CACHE, 'rb').read(0x4000))
    h[0x58:0x68] = h[0x190:0x1a0]             # its UUID is the one the main header asks for
    h[0x190:0x1a0] = bytes(16)
    for offset in (0x14, 0x1c, 0x13c, 0x18c, 0x1c4):    # mappings, images, mappings with slide, subcaches, images
        struct.pack_into('<I', h, offset, 0)
    struct.pack_into('<Q', h, 0x90, 0)        # images' text info
    open(path, 'wb').write(h)
    os.truncate(path, 0x60000)                # past where its code signature would be


def images():
    """(address, size, name) of every image's __TEXT, from the cache's images' text info."""
    h = open(CACHE, 'rb').read(0x40000)
    offset, count = struct.unpack_from('<QQ', h, 0x88)
    data = open(CACHE, 'rb')
    data.seek(offset)
    raw = data.read(count * 32)
    result = []
    for i in range(count):
        _, address, size, path = struct.unpack_from('<16sQII', raw, i * 32)
        data.seek(path)
        name = data.read(256).split(b'\0')[0].decode()
        result.append((address, size, os.path.basename(name)))
    return sorted(result)


def selector(declaration):
    """'- (void)setFoo:(id)foo bar:(int)b;' -> 'setFoo:bar:'"""
    text, depth, out = declaration.strip().rstrip(';')[1:].strip(), 0, ''
    for c in text:                            # drop every parenthesised type
        depth += c == '('
        if depth == 0:
            out += c
        depth -= c == ')'
    parts = re.findall(r'(\w+):', out)
    if parts:
        return ''.join(p + ':' for p in parts)
    words = out.split()
    return words[0] if words else text.split(')')[-1].strip() or '?'


def ipsw(*args):
    return subprocess.Popen([IPSW, 'dyld', *args, CACHE, '--no-color'], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, errors='replace', bufsize=1 << 20)


class Names:
    """Names for unslid addresses, looked up in bulk: the table is read once, in order."""

    def __init__(self):
        self.wanted = set()
        self.known = {}

    def want(self, address):
        self.wanted.add(address)

    def resolve(self):
        user = sorted(a for a in self.wanted if CACHE_LOW <= a < CACHE_HIGH)
        kernel = sorted(a for a in self.wanted if a >= KERNEL_LOW)
        if user:
            i, last = 0, None
            with open(TABLE) as table:
                for line in table:
                    start = int(line[:line.index(' ')], 16)
                    while i < len(user) and user[i] < start:
                        self.known[user[i]] = last
                        i += 1
                    if i == len(user):
                        break
                    _, end, image, name = line.rstrip('\n').split(' ', 3)
                    last = (image, name, start, int(end, 16))
            for a in user[i:]:
                self.known[a] = last
            ranges = images()
            starts = [start for start, _, _ in ranges]
            for a in user:
                found = self.known[a]
                if found and a >= found[3]:     # past its function: stubs of its image, or branch islands
                    j = bisect.bisect_right(starts, a) - 1
                    inside = j >= 0 and a < ranges[j][0] + ranges[j][1]
                    self.known[a] = (ranges[j][2] if inside else 'branch islands', 'stubs', a, a)
        if kernel:
            out = subprocess.run(['llvm-nm', '-n', KERNEL], capture_output=True, text=True).stdout
            symbols = [(int(f[0], 16), f[2]) for f in (l.split() for l in out.splitlines()) if len(f) == 3]
            starts = [s for s, _ in symbols]
            for a in kernel:
                j = bisect.bisect_right(starts, a) - 1
                self.known[a] = ('kernel', symbols[j][1], symbols[j][0]) if j >= 0 else None

    def describe(self, address):
        """'image name + offset' for an unslid address."""
        found = self.known.get(address)
        if found is None:
            return '?'
        return f'{found[0]} {found[1]}' + (f' + {address - found[2]:#x}' if address != found[2] else '')

    def function(self, address):
        """(image, name) of the function at an unslid address."""
        found = self.known.get(address)
        if found is None:
            return ('?', f'{address:#x}')
        return found[0], found[1]


def main():
    make_standin()
    names, ends = {}, {}
    print('dsc_symbols.py: function starts and Objective-C methods...', file=sys.stderr)
    cls, imp = None, None
    for line in ipsw('macho', '--all', '--starts', '--objc', '-V').stdout:
        start = re.fullmatch(r'(0x[0-9A-Fa-f]+)(-0x[0-9A-Fa-f]+)?\n?', line)     # a function (start-end with -V)
        if start:
            names.setdefault(int(start.group(1), 16), None)
            if start.group(2):
                ends[int(start.group(1), 16)] = int(start.group(2)[1:], 16)
        elif line.startswith('@interface '):
            m = re.match(r'@interface (\w+)(?: \((\w*)\))?', line)
            cls = m.group(1) + (f'({m.group(2)})' if m.group(2) else '') if m else None
        elif line.startswith('@end'):
            cls = None
        elif line.startswith('// 0x'):
            imp = int(line[3:].split()[0], 16)
            continue
        elif imp is not None and cls and line[:1] in '+-':
            names[imp] = f'{line[0]}[{cls} {selector(line)}]'
        imp = None
    print('dsc_symbols.py: exported symbols...', file=sys.stderr)
    for line in ipsw('symaddr').stdout:
        m = re.match(r'(0x[0-9a-f]+): \((?:regular|external\|__TEXT\.__text)[^)]*\)\s+(\S+)', line)
        if m:
            if int(m.group(1), 16) in names:     # a function's start (the others are data)
                names[int(m.group(1), 16)] = m.group(2)
    table = images()
    starts = [address for address, _, _ in table]
    with open(TABLE + '.new', 'w') as out:
        for address in sorted(names):
            i = bisect.bisect_right(starts, address) - 1
            if i < 0 or address >= table[i][0] + table[i][1]:
                continue
            end = min(ends.get(address, table[i][0] + table[i][1]), table[i][0] + table[i][1])
            out.write(f'{address:x} {end:x} {table[i][2]} {names[address] or f"func_{address:x}"}\n')
    os.replace(TABLE + '.new', TABLE)
    print(f'dsc_symbols.py: {TABLE}', file=sys.stderr)


if __name__ == '__main__':
    main()
