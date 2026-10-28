#!/usr/bin/env python3
"""Check tools/frames.py against synthetic touch_lat=1 logs.

What it pins down: a stall of 100 ms or more stays in the segment's headline numbers (count,
percentiles, shares) and only leaves the secondary under-100 ms lines; a segment of two frames counts
its one gap like any other; the printed thresholds are the ones the code counts at (18.0 and 34.5 ms,
with gaps of exactly 18.0 and 34.5 ms in the mix to keep the comparison strict); every frames-a-second
frames.py prints is checked on its own line, the headline ones as gap count over the span from the
segment's first to its last frame shown; percentiles follow the nearest rank frames.py documents;
the same lines read again from a second log of the same run change nothing; a run without gestures, a
gesture without frames and a frame the driver showed without its hand-over print and exit well; the
draw / gpu / idle / copy stages, the GPU waits and the core loads keep their old shape.
test_mutations_detected runs the S1/S2 counterexamples on copies of the tool source: the headline
rate with the frame count instead of the gap count, its denominator swapped to the under-100 ms sum,
and the threshold comparisons loosened all stop producing the pinned numbers.

Usage: python3 tools/test-frames.py
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

FRAMES = Path(__file__).resolve().parent / 'frames.py'


def steady(i, done):
    """A frame whose own stages cost little (0.5 ms of draw, 0.5 of GPU wait, 1 of copy): what the
    tests check sits in the gaps between frames."""
    return [f'tl: g {i} began {done - 9000} ended {done - 8500} 1',
            f'tl: s {i} at {done - 2000}',
            f'tl: v {i} ready {done - 1500} shown {done - 1000} done {done}']


def show(i, done, began, ended, swap, ready, shown):
    return [f'tl: g {i} began {began} ended {ended} 1',
            f'tl: s {i} at {swap}',
            f'tl: v {i} ready {ready} shown {shown} done {done}']


def stall_lines():
    """One touch, frames done at 1016000, 1032000, 1052000, 1102000, 1202000 and 1702000: gaps of
    16, 20, 50, 100 and 500 ms. 5 gaps over 1702000 - 1016000 = 686000 us -> 5e6 / 686000 = 7.3 a
    second (the 2.5 s window and the touch before the first frame sit outside that span); the 3 gaps
    under 100 ms cover 16000 + 20000 + 50000 = 86000 us -> 3e6 / 86000 = 34.9. Shares: 4 of 5 over
    18.0 ms = 80%, 3 of 5 over 34.5 ms = 60%."""
    lines = ['tl: p 1 0 32768 32768 at 1000000']
    for i, done in enumerate([1016000, 1032000, 1052000, 1102000, 1202000, 1702000]):
        lines += steady(100 + i, done)
    return lines


def boundary_lines():
    """One touch, frames done at 1018000, 1036000, 1054001, 1088501 and 1123002: gaps of exactly
    18000, 18001, 34500 and 34501 us. Counting only what is strictly over: over 18.0 ms -> 3 of 4 =
    75% (18001, 34500, 34501; 18000 is at the threshold), over 34.5 ms -> 1 of 4 = 25% (34501).
    4 gaps over 1123002 - 1018000 = 105002 us -> 4e6 / 105002 = 38.1 a second. Nearest rank over
    [18.0, 18.001, 34.5, 34.501] ms: p50 the 2nd value 18.001 (shown 18.0), p95 and p99 the 4th
    34.501 (34.5), max 34.5. Attribution over the 3 strictly-over gaps of steady frames: draw 0.5,
    gpu 0.5, copy 1.0 and idle = gap - 9000 + 1500 -> (10501 + 27000 + 27001) / 3 / 1000 = 21.5."""
    lines = ['tl: p 1 0 32768 32768 at 1000000']
    for i, done in enumerate([1018000, 1036000, 1054001, 1088501, 1123002]):
        lines += steady(50 + i, done)
    return lines


def repeat_touch_lines():
    """The same touch at 50%,50% twice: gaps 16/20/50 ms over 1016000..1102000 (86000 us -> 3e6 /
    86000 = 34.9) and 16/16 ms over 4016000..4048000 (32000 us -> 2e6 / 32000 = 62.5); together 5
    gaps over 16000 + 20000 + 50000 + 16000 + 16000 = 118000 us -> 5e6 / 118000 = 42.4."""
    lines = ['tl: p 1 0 32768 32768 at 1000000']
    for i, done in [(21, 1016000), (22, 1032000), (23, 1052000), (24, 1102000)]:
        lines += steady(i, done)
    lines += ['tl: p 1 0 32768 32768 at 4000000']
    for i, done in [(25, 4016000), (26, 4032000), (27, 4048000)]:
        lines += steady(i, done)
    return lines


def run(lines):
    """frames.py over one log, and over a second file holding the same lines again (as the run's
    live log repeats them): the counts must not move."""
    with tempfile.TemporaryDirectory() as d:
        run_log = Path(d) / 'run.log'
        live_log = Path(d) / 'live.log'
        run_log.write_text('\n'.join(lines) + '\n')
        live_log.write_text('\n'.join(lines) + '\n')
        out = subprocess.run([sys.executable, str(FRAMES), str(run_log)], capture_output=True, text=True)
        assert out.returncode == 0, f'frames.py failed: {out.stderr}'
        once = out.stdout
        out = subprocess.run([sys.executable, str(FRAMES), str(run_log), str(live_log)],
                             capture_output=True, text=True)
        assert out.returncode == 0, f'frames.py over two logs failed: {out.stderr}'
        assert out.stdout == once, 'lines read again from the run\'s live log changed the counts'
    return once


def one_line(out, start):
    """The single output line that starts with `start`: rates are checked line by line so the
    headline one cannot be satisfied by the secondary under-100 ms figure printed on the same line."""
    hits = [l for l in out.splitlines() if l.startswith(start)]
    assert len(hits) == 1, f'want one line starting {start!r}, got {len(hits)} in:\n{out}'
    return hits[0]


def mutated_out(old, new, count, lines):
    """The check run against a copy of the frames.py source with `old` replaced by `new`:
    the production file itself stays as it is."""
    source = FRAMES.read_text()
    assert source.count(old) == count, f'mutation target {old!r} found {source.count(old)}, want {count}'
    with tempfile.TemporaryDirectory() as d:
        tool = Path(d) / 'frames-mutated.py'
        tool.write_text(source.replace(old, new))
        log = Path(d) / 'run.log'
        log.write_text('\n'.join(lines) + '\n')
        out = subprocess.run([sys.executable, str(tool), str(log)], capture_output=True, text=True)
        assert out.returncode == 0, f'the mutated tool failed: {out.stderr}'
    return out.stdout


def test_long_stalls_counted():
    # one touch, then frames 16, 20, 50, 100 and 500 ms apart (stall_lines for the expected numbers);
    # a button with no frame after it; a frame the driver reported without its hand-over; and a line
    # no number fits into
    lines = stall_lines()
    lines += ['tl: p 4 115 0 0 at 3500000',
              'tl: v 900 ready 4000000 shown 4001000 done 4002000',
              'tl: g 1 began x ended y 3']
    out = run(lines)
    for want in [
        "2 gestures, 6 frames copied into the panel's memory",
        '1 of the 7 frames the display driver reported are left out',
        'the hardware scanout and presentation are not measured',
        '5 steps; p50 50.0, p95 500.0, p99 500.0, max 500.0 ms (nearest rank)',
        '2 of them 100 ms or more',
        'button 115: 0 frames',
        'the slow ones: draw  0.5, gpu  0.5, idle before 160.0, copy 1.0 ms',
        'no full split of the gap',
        'all gestures: 5 steps; p50 50.0, p95 500.0, p99 500.0, max 500.0 ms (nearest rank)',
    ]:
        assert want in out, f'missing {want!r} in:\n{out}'
    seg = one_line(out, ' 1.')
    assert ' 7.3 a second' in seg, f'the headline rate wants 5 gaps over 686000 us = 7.3:\n{out}'
    assert 'intervals under 100 ms: 3 at 34.9 a second' in seg, \
        f'the secondary rate wants 3 gaps over 86000 us = 34.9 on the same line:\n{out}'
    assert ' 7.3 a second' in one_line(out, 'all gestures'), \
        f'the all-gestures headline rate wants the same 5 gaps over 686000 us = 7.3:\n{out}'
    for scope, want in [(seg, [r'over 18\.0 ms\s+80%', r'over 34\.5 ms\s+60%']),
                        (one_line(out, 'all gestures'), [r'over 18\.0 ms\s+80%', r'over 34\.5 ms\s+60%'])]:
        for pattern in want:
            assert re.search(pattern, scope), f'the shares are not 80% and 60% of the 5 gaps on:\n{scope}'
    assert 'over 16.7' not in out and 'over 33 ms' not in out, f'an old threshold label survived:\n{out}'


def test_slow_frame_attribution():
    # five frames done at 1016000, 1032000, 1072000, 1088000 and 1104000, one of them 40 ms after
    # the previous: 4 gaps over 1104000 - 1016000 = 88000 us -> 4e6 / 88000 = 45.5 a second. frames.py
    # reports for the slow frame 20 ms of drawing, 5 ms between the hand-over and the GPU being done,
    # 10 ms before the window server began it and 1 ms of copying: stages read at the frame's lines,
    # covering part of the 40 ms, not a split that adds up to it
    lines = ['tl: p 1 0 0 39321 at 1000000']
    for i, done in [(11, 1016000), (12, 1032000), (14, 1088000), (15, 1104000)]:
        lines += steady(i, done)
    lines += show(13, 1072000, 1040500, 1060500, 1065000, 1070000, 1071000)
    lines += ['tl: f EGL 5000 at 1030000', 'tl: u at 1050000 0:80 7:40']
    out = run(lines)
    for want in [
        '4 steps; p50 16.0, p95 40.0, p99 40.0, max 40.0 ms (nearest rank)',
        'the slow ones: draw 20.0, gpu  5.0, idle before  10.0, copy 1.0 ms',
        '1 waits for the GPU (5 ms)',
        'cores busy 7:40% 0:80%',
    ]:
        assert want in out, f'missing {want!r} in:\n{out}'
    assert '45.5 a second' in one_line(out, ' 1.'), \
        f'the headline rate wants 4 gaps over 88000 us = 45.5:\n{out}'
    assert re.search(r'over 18\.0 ms\s+25%', one_line(out, ' 1.')), \
        f'the one gap over 18.0 ms is not 25% of 4:\n{out}'
    assert 'intervals under 100 ms' not in out, f'a split this log never needed got printed:\n{out}'


def test_short_windows():
    # a segment of two frames 500 ms apart still counts its one gap: 1 gap over 500000 us =
    # 2.0 a second, where counting frames instead of gaps would print 4.0; a segment of one frame
    # says so; two segments of the same touch aggregate, the one-frame one taking no part in it
    lines = ['tl: p 1 0 32768 32768 at 1000000']
    lines += steady(31, 1016000) + steady(32, 1516000)
    lines += ['tl: p 1 0 32768 32768 at 4000000']
    lines += steady(33, 4016000) + steady(34, 4032000)
    lines += ['tl: p 1 0 32768 32768 at 7000000'] + steady(35, 7016000)
    out = run(lines)
    for want in [
        '1 steps; p50 500.0, p95 500.0, p99 500.0, max 500.0 ms (nearest rank)',
        'intervals under 100 ms: 0',
        '7.000 s touch at 50%,50%: 1 frames',
        'the same gestures together:',
        '2 times, 2 steps; p50 16.0, p95 500.0, p99 500.0, max 500.0 ms (nearest rank)',
        '1 of them 100 ms or more',
        'all gestures: 2 steps; p50 16.0, p95 500.0, p99 500.0, max 500.0 ms (nearest rank)',
    ]:
        assert want in out, f'missing {want!r} in:\n{out}'
    assert ' 2.0 a second' in one_line(out, ' 1.'), \
        f'two frames 500000 us apart want 1 gap / 500000 us = 2.0:\n{out}'
    assert re.search(r'over 18\.0 ms\s+100%', one_line(out, ' 1.')), \
        f'the only gap of the first segment is not 100%:\n{out}'


def test_same_gesture_together():
    # the same touch twice (repeat_touch_lines for the expected numbers): its second block sums
    # both segments, so the aggregate headline rate is the 5 gaps over the 118000 us they cover
    out = run(repeat_touch_lines())
    for want in [
        'the same gestures together:',
        '2 times, 5 steps; p50 16.0, p95 50.0, p99 50.0, max 50.0 ms (nearest rank)',
        '0 of them 100 ms or more',
        'all gestures: 5 steps; p50 16.0, p95 50.0, p99 50.0, max 50.0 ms (nearest rank)',
    ]:
        assert want in out, f'missing {want!r} in:\n{out}'
    assert '42.4 a second' in one_line(out, '  touch at 50%,50%'), \
        f'the aggregate headline rate wants 5 gaps over 118000 us = 42.4:\n{out}'
    assert '42.4 a second' in one_line(out, 'all gestures'), \
        f'the all-gestures headline rate wants 5 gaps over 118000 us = 42.4:\n{out}'


def test_threshold_boundaries():
    # gaps of exactly 18.0 and exactly 34.5 ms next to ones 1 us longer: only what is strictly over
    # counts (boundary_lines for the expected numbers)
    out = run(boundary_lines())
    for want in [
        '4 steps; p50 18.0, p95 34.5, p99 34.5, max 34.5 ms (nearest rank)',
        'the slow ones: draw  0.5, gpu  0.5, idle before  21.5, copy 1.0 ms',
    ]:
        assert want in out, f'missing {want!r} in:\n{out}'
    seg = one_line(out, ' 1.')
    whole = one_line(out, 'all gestures')
    assert '38.1 a second' in seg, f'the headline rate wants 4 gaps over 105002 us = 38.1:\n{out}'
    assert '38.1 a second' in whole, \
        f'the all-gestures headline rate wants 4 gaps over 105002 us = 38.1:\n{out}'
    # line by line: a loosened comparison moves the segment line's shares to 100% and 50%, and the
    # all-gestures line has its own share computation that must move the same way
    for scope in (seg, whole):
        assert re.search(r'over 18\.0 ms\s+75%', scope), \
            f'18001, 34500 and 34501 of 4 gaps over 18.0 ms is 75%; counting 18000 would print 100:\n{scope}'
        assert re.search(r'over 34\.5 ms\s+25%', scope), \
            f'34501 alone of 4 gaps over 34.5 ms is 25%; counting 34500 would print 50:\n{scope}'
    assert 'intervals under 100 ms' not in out, f'every gap here is under 100 ms, no split line belongs:\n{out}'


def test_mutations_detected():
    # the pinned numbers above must stop coming out when the production logic moves (review S1/S2):
    # a headline numerator counting frames instead of gaps, a headline denominator swapped to the
    # under-100 ms sum, and the threshold comparisons loosened or shifted while the printed labels
    # stay 18.0 and 34.5
    seg_rate = "f'; {len(values) * 1e6 / whole if whole else 0:4.1f} a second; '"
    seg_head = "s {what}: ' + gap_stats(values) +\n                " + seg_rate
    checks = [
        ('headline numerator, segment line (S1)',
         seg_head, seg_head.replace('len(values)', '(len(values) + 1)'),
         1, stall_lines(), ' 1.', ' 7.3 a second'),
        ('headline denominator, segment line (S1)',
         seg_head, seg_head.replace('/ whole', '/ span'),
         1, stall_lines(), ' 1.', ' 7.3 a second'),
        ('headline numerator, aggregate line (S1)',
         '                  ' + seg_rate,
         '                  ' + seg_rate.replace('len(values)', '(len(values) + 1)'),
         1, repeat_touch_lines(), '  touch at 50%,50%', '42.4 a second'),
        ('headline numerator, all-gestures line (S1)',
         "f'; {len(all_gaps) * 1e6 / whole if whole else 0:4.1f} a second; '",
         "f'; {(len(all_gaps) + 1) * 1e6 / whole if whole else 0:4.1f} a second; '",
         1, stall_lines(), 'all gestures', ' 7.3 a second'),
        ('threshold counted at >= (S2)',
         'if g > threshold', 'if g >= threshold',
         1, boundary_lines(), ' 1.', 'over 18.0 ms  75%'),
        ('slow-frame set counted at >= (S2)',
         'if g[0] > OVER_ONE]', 'if g[0] >= OVER_ONE]',
         1, boundary_lines(), None, 'idle before  21.5'),
        ('slow-frame threshold shifted under its label (S2)',
         'if g[0] > OVER_ONE]', 'if g[0] > OVER_ONE - 500]',
         1, boundary_lines(), None, 'idle before  21.5'),
        ('share threshold shifted under its label (S2)',
         'over_share(values, OVER_ONE)', 'over_share(values, OVER_ONE - 500)',
         2, boundary_lines(), ' 1.', 'over 18.0 ms  75%'),
    ]
    for what, old, new, count, lines, prefix, want in checks:
        out = mutated_out(old, new, count, lines)
        if prefix is not None:
            out = one_line(out, prefix)
        assert want not in out, f'{what}: {want!r} still printed, the check cannot tell it apart:\n{out}'
    # the loose threshold comparison moves the shares of both boundary samples, so the check above
    # is scoped to one: pin that it moved away from the strict > result, 75% and 25%
    loose = mutated_out('if g > threshold', 'if g >= threshold', 1, boundary_lines())
    assert not re.search(r'over 18\.0 ms\s+75%', loose) and re.search(r'over 18\.0 ms\s+100%', loose), \
        f'>= did not move 18.0 ms counting to 100%:\n{loose}'
    assert not re.search(r'over 34\.5 ms\s+25%', loose) and re.search(r'over 34\.5 ms\s+50%', loose), \
        f'>= did not move 34.5 ms counting to 50%:\n{loose}'


def test_nothing_to_count():
    # no gesture at all, and lines no number fits into: frames.py says so and exits well
    out = run(['hello', 'tl: p x y z at w', 'not a tl line at all'])
    assert "0 gestures, 0 frames copied into the panel's memory, 0 waits" in out, out
    assert 'all gestures:' not in out, f'a run without steps printed an all-gestures line:\n{out}'


def main():
    for test in (test_long_stalls_counted, test_slow_frame_attribution, test_short_windows,
                 test_same_gesture_together, test_threshold_boundaries, test_mutations_detected,
                 test_nothing_to_count):
        test()
        print(f'PASS: {test.__name__}')


if __name__ == '__main__':
    main()
