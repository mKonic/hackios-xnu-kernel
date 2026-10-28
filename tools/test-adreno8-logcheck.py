#!/usr/bin/env python3
"""Negative and positive self-tests for the shared MP_ASSIGN log parser.

Each case feeds one log stream through adreno8_logcheck.check and asserts
accept or reject. The six malformed-stream counterexamples from the fixed
review of 6012234 (region lines after a snapshot head, the before line after
a snapshot head, snapshot records after the returned line, a region index
jump, a malformed scmsnap line, a malformed MP_ASSIGN line) must all be
rejected, and the cases that already rejected must keep rejecting.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adreno8_logcheck as logcheck

ADDRS = ["0x808c0000", "0x808e0000", "0x80900000"]
PAGE = 4096


def table_batch(batch, call, regions):
    """Baseline lines for one landed table batch with `regions` regions."""
    page = 0x806c0000 + 0x1000 * batch
    lines = [f"adreno8: MP_ASSIGN plan: batch {batch} table, permissions "
             f"HLOS read/write and CP_PIXEL read, argument page {page:#x}, {regions} regions"]
    for i in range(regions):
        lines.append(f"adreno8: MP_ASSIGN plan: batch {batch} region {i} addr "
                     f"{ADDRS[i % 3]} len {PAGE}")
    lines.append(f"adreno8: MP_ASSIGN batch {batch} table: {regions} regions, 2 permission "
                 f"records, {call} top-level SCM calls before")
    lines.append(f"scmsnap call {call} argpage {page:#x} regions_bytes {regions * 16} "
                 f"sources {page + 512:#x} sources0 3 extended {page + 640:#x} "
                 f"ext 0x4 {page + 576:#x} {48:#x} 0")
    for i in range(regions):
        lines.append(f"scmsnap call {call} region {i} addr {ADDRS[i % 3]} len {PAGE}")
    lines.append(f"scmsnap call {call} perm 0 vmid 3 perm 6 ctx 0 ctxsize 0 unused 0")
    lines.append(f"scmsnap call {call} perm 1 vmid 10 perm 4 ctx 0 ctxsize 0 unused 0")
    lines.append(f"adreno8: MP_ASSIGN batch {batch} table returned 0, answer 0, "
                 f"{call + 1} top-level SCM calls (A6 interrupted continuation SMCs not counted)")
    return lines


BASE = table_batch(0, 0, 3)


def move(lines, predicate, after):
    moved = [l for l in lines if predicate(l)]
    remain = [l for l in lines if not predicate(l)]
    pos = next(i for i, l in enumerate(remain) if after(l)) + 1
    return remain[:pos] + moved + remain[pos:]


def retry_group(lines):
    """Insert a second complete snapshot group (EBUSY retry) before the returned line."""
    dup = [l.replace("scmsnap call 0", "scmsnap call 1") for l in lines
           if l.startswith("scmsnap call 0")]
    pos = next(i for i, l in enumerate(lines) if "returned" in l)
    after = lines[pos].replace("1 top-level SCM calls", "2 top-level SCM calls")
    return lines[:pos] + dup + [after] + lines[pos + 1:]


CASES = [
    ("baseline", BASE, ["table"], True),
    # The six counterexamples the fixed review proved were accepted although
    # they must fail; every one must now reject.
    ("regions-after-snapshot-head",
     move(BASE, lambda l: l.startswith("adreno8: MP_ASSIGN plan: batch 0 region"),
          lambda l: l.startswith("scmsnap call 0 argpage")), ["table"], False),
    ("before-after-snapshot-head",
     move(BASE, lambda l: l.endswith("calls before"),
          lambda l: l.startswith("scmsnap call 0 argpage")), ["table"], False),
    ("snapshot-details-after-return",
     move(BASE, lambda l: l.startswith("scmsnap call 0 region")
          or l.startswith("scmsnap call 0 perm"), lambda l: "returned" in l),
     ["table"], False),
    ("wrong-region-index",
     [l.replace("region 0 addr", "region 99 addr") for l in BASE], ["table"], False),
    ("malformed-scmsnap", BASE + ["scmsnap call 99 garbage"], ["table"], False),
    ("malformed-mpassign", BASE + ["adreno8: MP_ASSIGN batch 1 INVALID returned 0"],
     ["table"], False),
    # Cases that already rejected must keep rejecting.
    ("wrong-numeric-permission",
     [l.replace("vmid 3 perm 6", "vmid 3 perm 7") for l in BASE], ["table"], False),
    ("missing-region", [l for l in BASE
                        if not l.startswith("adreno8: MP_ASSIGN plan: batch 0 region 2")],
     ["table"], False),
    ("duplicate-plan", [BASE[0]] + BASE, ["table"], False),
    ("wrong-role", [l.replace("batch 0 table:", "batch 0 data:") for l in BASE],
     ["table"], False),
    ("future-plan-after-failure",
     [l.replace("returned 0, answer 0,", "returned -5, answer 0x5,") for l in BASE]
     + [BASE[0].replace("batch 0", "batch 1")], ["table", "table"], False),
    # Additional state-machine and external-role checks.
    ("ebusy-retry-two-groups", retry_group(BASE), ["table"], True),
    ("head-before-before",
     move(BASE, lambda l: l.startswith("scmsnap call 0 argpage"),
          lambda l: l.startswith("adreno8: MP_ASSIGN plan: batch 0 region 2")),
     ["table"], False),
    ("wrong-sources-value", [l.replace("sources0 3", "sources0 4") for l in BASE],
     ["table"], False),
    ("wire-region-mismatch", [l.replace("scmsnap call 0 region 1 addr 0x808e0000",
                                        "scmsnap call 0 region 1 addr 0x808f0000")
                              for l in BASE], ["table"], False),
    ("scenario-role-mismatch", BASE, ["data"], False),
]


def run():
    failures = []
    for name, lines, roles, accepted in CASES:
        text = "\n".join(lines) + "\n"
        try:
            logcheck.check(text, PAGE, roles)
            outcome = True
            message = ""
        except AssertionError as exc:
            outcome = False
            message = str(exc)
        if outcome is not accepted:
            failures.append(f"{name}: expected {'accept' if accepted else 'reject'}, got "
                            f"{'accept' if outcome else 'reject ' + message}")
        print(f"{name}: {'accepted' if outcome else 'rejected'} "
              f"({'expected' if outcome is accepted else 'UNEXPECTED'})")
    assert not failures, "\n".join(failures)
    print(f"logcheck self-test: {len(CASES)} cases: PASS")


if __name__ == "__main__":
    run()
