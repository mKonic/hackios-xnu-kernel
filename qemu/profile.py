#!/usr/bin/env python3
"""Where the guest's processors go: a report of the samples QEMU's sampling profiler took.

usage: profile.py SAMPLES LOG [--from SECONDS] [--to SECONDS] [--process NAME] [--thread TEXT] [-n LINES]
                  [--callers TEXT]

SAMPLES is what QEMU's sampler plugin wrote ("QEMU_PROFILE=FILE qemu/run-ios.sh ..."), LOG the console log of
that boot with the
names of the threads in it ("qemu/touch.py stacks threads" asks the kernel for them, best towards the end:
threads that ended before go unnamed). --from and --to pick a stretch of the samples, in seconds since
QEMU started; --process and --thread only take what runs in that process or in threads with TEXT in their
name.

The report: the share of each process and thread, then the functions the processors were in ("self") and
the functions that were on the stack ("total", each counted once per sample). With --callers TEXT, the
stacks the functions with TEXT in their name were on instead. User space is named from
ios165/dsc-functions.txt (qemu/dsc_symbols.py makes it), the kernel from its build.
"""
import argparse
import bisect
import collections
import os
import re
import sys

import dsc_symbols



def read_threads(log):
    """tid -> (pid, process, thread name); pid -> shared cache slide; the kernel's slide."""
    threads, slides, kernel_slide = {}, {}, 0
    for line in open(log, errors='replace'):
        m = re.search(r'threads: (.*)', line)
        if not m:
            continue
        text = m.group(1).rstrip()
        if m2 := re.match(r'kernel slide (0x[0-9a-f]+)', text):
            kernel_slide = int(m2.group(1), 16)
        elif m2 := re.match(r'process (\d+) (\S+), shared cache slide (0x[0-9a-f]+)', text):
            slides[int(m2.group(1))] = int(m2.group(3), 16)
        elif m2 := re.match(r'(\d+) (\d+) (\S+) ?(.*)', text):
            threads[int(m2.group(1)) & 0xffffffff] = (int(m2.group(2)), m2.group(3), m2.group(4))
    return threads, slides, kernel_slide


def main():
    p = argparse.ArgumentParser()
    p.add_argument('samples')
    p.add_argument('log')
    p.add_argument('--from', dest='start', type=float, default=0)
    p.add_argument('--to', dest='end', type=float, default=1e12)
    p.add_argument('--process')
    p.add_argument('--thread')
    p.add_argument('-n', type=int, default=30)
    p.add_argument('--callers', help='the stacks the functions with this in their name were called from')
    p.add_argument('--depth', type=int, default=0, help='with --callers: that many frames of each stack, so that they add up')
    a = p.parse_args()
    if not os.path.exists(dsc_symbols.TABLE):
        sys.exit(f'profile.py: no {dsc_symbols.TABLE}; qemu/dsc_symbols.py makes it')
    threads, slides, kernel_slide = read_threads(a.log)
    # one shared region for all, as a rule: the threads that ended before the names were asked for get its slide
    common = collections.Counter(slides.values()).most_common(1)[0][0] if slides else 0
    if not threads:
        print('profile.py: no thread names in the log ("qemu/touch.py stacks threads" while it runs)', file=sys.stderr)

    samples, idle = [], None
    for line in open(a.samples):
        f = line.split()
        if f[0] == 'idle':
            idle = list(map(int, f[1:]))
            continue
        ms = int(f[0])
        if not a.start * 1000 <= ms < a.end * 1000:
            continue
        tid, el = int(f[2]), int(f[3])
        pid, process, tname = threads.get(tid, (-1, f'thread {tid}' if tid else 'kernel', ''))
        if el > 0 and tid == 0:
            process = 'kernel'
        if a.process and process != a.process:
            continue
        if a.thread and a.thread not in tname:
            continue
        slide = kernel_slide if el > 0 else slides.get(pid, common)
        stack = [int(x, 16) - slide for x in f[4:]]
        samples.append((tid, el, pid, process, tname, stack))

    names = dsc_symbols.Names()
    for _, _, _, _, _, stack in samples:
        for address in stack:
            names.want(address)
    names.resolve()

    def label(image, name):
        return f'{image} {name}'

    total = len(samples)
    if not total:
        sys.exit('profile.py: no samples there')
    by_process = collections.Counter(s[3] for s in samples)
    by_thread = collections.Counter((s[3], s[2], s[4] or f'thread {s[0]}') for s in samples)
    by_el = collections.Counter(s[1] for s in samples)
    selfs, totals, images = collections.Counter(), collections.Counter(), collections.Counter()
    chains = collections.Counter()
    for tid, el, pid, process, tname, stack in samples:
        pc, lr, frames = stack[0], stack[1], stack[2:]
        here = names.function(pc)
        chain = [here]
        caller = names.function(lr)
        # the return address in x30 is the caller's for a leaf function (no frame of its own yet)
        if caller != here and (not frames or names.function(frames[0]) != caller):
            chain.append(caller)
        chain += [names.function(x) for x in frames]
        if a.callers:
            at = next((i for i, f in enumerate(chain) if a.callers in label(*f)), None)
            if at is not None:
                chains[tuple(label(*f) for f in chain[at:at + a.depth if a.depth else None])] += 1
        selfs[label(*here)] += 1
        images[here[0]] += 1
        for function in set(chain):
            totals[label(*function)] += 1

    def show(title, counter, n):
        print(f'\n{title}')
        for key, count in counter.most_common(n):
            print(f'  {100 * count / total:5.1f}%  {key}')

    span = (max(int(l.split()[0]) for l in open(a.samples) if l[0].isdigit()) / 1000) if a.end >= 1e12 else a.end
    print(f'{total} samples from {a.start:g} s to {span:g} s; {100 * by_el[0] / total:.1f}% in user space, '
          f'{100 * (total - by_el[0]) / total:.1f}% in the kernel' + (f'; idle per vCPU (whole run): {idle}' if idle else ''))
    if a.callers:
        found = sum(chains.values())
        print(f'{a.callers}: in {100 * found / total:.1f}% of the samples; its most common stacks:')
        for chain, count in chains.most_common(a.n):
            print(f'\n  {100 * count / total:5.1f}%')
            for function in chain:
                print(f'          {function}')
        return
    show('processes', by_process, 15)
    show('threads', collections.Counter({f'{p}[{pid}] {t}': c for (p, pid, t), c in by_thread.items()}), 20)
    show('images (self)', images, 15)
    show('functions (self)', selfs, a.n)
    show('functions (total)', totals, a.n)


if __name__ == '__main__':
    main()
