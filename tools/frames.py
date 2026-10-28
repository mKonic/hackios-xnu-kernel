#!/usr/bin/env python3
"""
frames.py RUN.log [MORE.log ...]: how evenly the frames of a run's gestures came, and what held up the slow ones.
(More logs: the live ones of the same run, XL_FOLLOW=N XL_FOLLOW_KEEP=1, which keep what the run's log loses.)

Reads the "tl:" lines a run with boot-arg touch_lat=1 writes (see tools/latency.py for what each
says): the samples and buttons of rwtouch (tl: p), the window server's frames (tl: g: began, ended),
their hand-over (tl: s), the display driver's GPU wait and copy (tl: v), and every wait of a program
for the GPU of a millisecond or more (tl: f). Every gesture -- from a touch going down, or a button,
to the next one or 2.5 s later -- is a segment; for each:

  every frame-to-frame time of the segment (as the pixels reached the panel's memory), counted whole:
  how many there are, their p50/p95/p99 and the longest of them, the frames a second over the span
  between the segment's first and last frame shown (the touch to the first frame and the tail of the
  window are outside that span), and the share over 18.0 ms and over 34.5 ms, experience thresholds
  referenced to the 60 Hz frame period and its double;
  of those times the ones under 100 ms, printed apart as a secondary statistic: their count and the
  frames a second over the time they cover; the split at 100 ms reads nothing into the longer gaps;
  with boot-arg cpuload=MS (tl: u), how busy each processor core was meanwhile, in percent;
  and for the frames over 18.0 ms, where the time between the previous frame and this one went:
    draw      the window server drawing it (its first draw to its glFlush done: CPU, Mesa included)
    gpu       the GPU after the hand-over (SwapEnd to done)
    idle      the window server not drawing at all before it began (waiting for a commit, a fence, ...)
    copy      the display driver's copy into the panel's memory
  the four are what the lines of those frames show: they cover part of a gap between frames, need not
  add up to it and say no more about the panel than the times above do.

The thresholds are as the coordinator asked for them; 18.0 rather than 16.7 because a frame of a steady
60 comes 16.6 to 16.8 ms after the one before.

Times are copy-completion timestamps: when a frame's pixels reached the panel's memory (the display
driver's copy, tl: v done). The hardware scanout of that memory and the presentation of the picture
are not measured here.
"""
import sys

OVER_ONE, OVER_TWO, SEGMENT_US, UNDER_MAX = 18000, 34500, 2500000, 100000
LABEL_ONE, LABEL_TWO = f'{OVER_ONE / 1000:.1f}', f'{OVER_TWO / 1000:.1f}'    # printed as the real thresholds


def percentile(sorted_ms, p):
    """The p-th percentile of times already sorted and in ms, by the nearest rank: the value in
    position ceil(p * n / 100) of the sorted list, which is one of the measured times."""
    if not sorted_ms:
        return None
    return sorted_ms[max(1, -(-p * len(sorted_ms) // 100)) - 1]


def lines_of(paths):
    seen, out = set(), []
    for path in paths:
      with open(path, 'rb') as f:
        for raw in f:
            text = raw.decode('utf-8', 'replace')
            at = text.find('tl: ')
            if at < 0:
                continue
            line = text[at:].strip()
            if line not in seen:
                seen.add(line)
                out.append(line)
    return out


def gap_stats(gaps_us):
    """One phrase: how many frame-to-frame times, their p50/p95/p99 and the longest, in ms;
    percentiles by nearest rank (see percentile)."""
    ms = sorted(g / 1000 for g in gaps_us)
    return (f'{len(ms)} steps; p50 {percentile(ms, 50):.1f}, p95 {percentile(ms, 95):.1f}, '
            f'p99 {percentile(ms, 99):.1f}, max {ms[-1]:.1f} ms (nearest rank)')


def over_share(gaps_us, threshold):
    return 100 * sum(1 for g in gaps_us if g > threshold) / max(1, len(gaps_us))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    gestures, frames, swaps, shows, waits, loads = [], [], {}, {}, [], []
    for line in lines_of(sys.argv[1:]):
        w = line.split()
        try:
            if w[1] == 'p' and len(w) >= 8 and int(w[2]) in (1, 4):
                gestures.append((int(w[7]), int(w[2]), int(w[3]), int(w[4]), int(w[5])))
            elif w[1] == 'g' and len(w) >= 8:
                frames.append((int(w[4]), int(w[6]), int(w[7])))
            elif w[1] == 's' and len(w) >= 5:
                swaps[int(w[2])] = int(w[4])
            elif w[1] == 'v' and len(w) >= 9:
                shows[int(w[2])] = (int(w[4]), int(w[6]), int(w[8]))
            elif w[1] == 'f' and len(w) >= 6:
                waits.append((int(w[5]), w[2], int(w[3])))
            elif w[1] == 'u' and len(w) >= 4:
                loads.append((int(w[3]), {int(a): int(b) for a, b in (x.split(':') for x in w[4:])}))
        except ValueError:
            pass
    gestures.sort()
    frames.sort(key=lambda f: f[1])
    # every frame copied into the panel's memory, with the drawing it came from
    timeline = []
    for sid in sorted(shows):
        if sid not in swaps:
            continue
        swap = swaps[sid]
        drawn = [f for f in frames if f[1] <= swap]
        if not drawn:
            continue
        began, ended, draws = drawn[-1]
        ready, shown, done = shows[sid]
        timeline.append(dict(began=began, ended=ended, swap=swap, ready=ready, shown=shown, done=done))
    timeline.sort(key=lambda f: f['done'])
    all_gaps = []
    kinds, previous = {}, ''          # the same gesture done more than once: its frames together
    print(f'frames.py: {len(gestures)} gestures, {len(timeline)} frames copied into the panel\'s memory, {len(waits)} waits for the GPU '
          f'of 1 ms or more')
    if len(shows) > len(timeline):
        print(f'  {len(shows) - len(timeline)} of the {len(shows)} frames the display driver reported are left out: '
              f'their hand-over (tl: s) or their drawing (tl: g) is not in these logs')
    print("  a frame's time is when the copy of its pixels into the panel's memory completed (tl: v "
          'done); the hardware scanout and presentation are not measured')
    for i, (start, kind, button, x, y) in enumerate(gestures):
        end = min(gestures[i + 1][0] if i + 1 < len(gestures) else start + SEGMENT_US, start + SEGMENT_US)
        chosen = [f for f in timeline if start <= f['done'] < end]
        gaps = [(b['done'] - a['done'], a, b) for a, b in zip(chosen, chosen[1:]) if b['done'] >= a['done']]
        what = f'button {button}' if kind == 4 else f'touch at {x / 655.35:.0f}%,{y / 655.35:.0f}%'
        key = f'{what} after {previous}' if kind == 4 else what
        previous = what
        if not gaps:
            print(f'{i + 1:2}. {start / 1e6:9.3f} s {what}: {len(chosen)} frames')
            continue
        values = [g[0] for g in gaps]
        whole = sum(values)          # the span from the first to the last frame shown: the window edges are outside it
        under = [g for g in gaps if g[0] < UNDER_MAX]    # secondary split at 100 ms only; no claim about the longer gaps
        span = sum(g[0] for g in under)
        over1 = [g for g in gaps if g[0] > OVER_ONE]
        over2 = [g for g in gaps if g[0] > OVER_TWO]
        all_gaps += values
        k = kinds.setdefault(key, [0, []])
        k[0] += 1
        k[1] += values
        line = (f'{i + 1:2}. {start / 1e6:9.3f} s {what}: ' + gap_stats(values) +
                f'; {len(values) * 1e6 / whole if whole else 0:4.1f} a second; '
                f'over {LABEL_ONE} ms {over_share(values, OVER_ONE):3.0f}%, '
                f'over {LABEL_TWO} ms {over_share(values, OVER_TWO):3.0f}%')
        if len(under) < len(gaps):
            line += (f'; {len(gaps) - len(under)} of them {UNDER_MAX // 1000} ms or more; '
                     f'intervals under {UNDER_MAX // 1000} ms: {len(under)}'
                     + (f' at {len(under) * 1e6 / span:4.1f} a second' if span else ''))
        if over1:
            # where the slow frames' time went (means over them)
            def mean(values_ms):
                return sum(values_ms) / len(values_ms) / 1000 if values_ms else 0
            draw = mean([b['ended'] - b['began'] for _, a, b in over1])
            gpu = mean([b['ready'] - b['swap'] for _, a, b in over1])
            idle = mean([b['began'] - a['ready'] for _, a, b in over1])
            copy = mean([b['done'] - b['shown'] for _, a, b in over1])
            line += (f'; the slow ones: draw {draw:4.1f}, gpu {gpu:4.1f}, idle before {idle:5.1f}, '
                     f'copy {copy:3.1f} ms (what the lines of those frames show: no full split of the gap)')
            stalls = [w for w in waits if start <= w[0] < end]
            if stalls:
                line += f'; {len(stalls)} waits for the GPU ({sum(w[2] for w in stalls) / 1000:.0f} ms)'
        # the load samples that end within the movement (each covers the cpuload=MS before it)
        within = [l for t, l in loads if chosen[0]['done'] < t <= chosen[-1]['done']]
        if within:
            cores = sorted({c for l in within for c in l}, reverse=True)
            line += '; cores busy ' + ' '.join(f'{c}:{sum(l.get(c, 0) for l in within) / len(within):.0f}%' for c in cores)
        print(line)
    if any(k[0] > 1 for k in kinds.values()):
        print('the same gestures together:')
        for key, (times, values) in kinds.items():
            whole = sum(values)
            print(f'  {key:40} {times} times, ' + gap_stats(values) +
                  f'; {len(values) * 1e6 / whole if whole else 0:4.1f} a second; '
                  f'over {LABEL_ONE} ms {over_share(values, OVER_ONE):3.0f}%, '
                  f'over {LABEL_TWO} ms {over_share(values, OVER_TWO):3.0f}%; '
                  f'{sum(1 for g in values if g >= UNDER_MAX)} of them {UNDER_MAX // 1000} ms or more')
    if all_gaps:
        whole = sum(all_gaps)
        print('all gestures: ' + gap_stats(all_gaps) +
              f'; {len(all_gaps) * 1e6 / whole if whole else 0:4.1f} a second; '
              f'over {LABEL_ONE} ms {over_share(all_gaps, OVER_ONE):.0f}%, '
              f'over {LABEL_TWO} ms {over_share(all_gaps, OVER_TWO):.0f}%; '
              f'{sum(1 for g in all_gaps if g >= UNDER_MAX)} of them {UNDER_MAX // 1000} ms or more')


if __name__ == '__main__':
    main()
