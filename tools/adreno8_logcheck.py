"""Shared parser and wire-comparison assertions for the adreno8 MP_ASSIGN staged-batch log.

The production flush prints the plan header, one line per staged region, the
before/after call lines, and the retain line; the test fakes print one
`scmsnap` group per SCM entry to the SAME stdout stream, so the observed
interleaving is the true event order. check() walks the stream once and
updates the open-batch / open-snapshot state line by line: it never defers
ordering to a final pass. Every line carrying a controlled prefix must match
its full pattern; a malformed or unknown controlled line fails immediately.
Coverage is the host fixture only: no real kernel, no real SMC, no hardware
cache visibility.
"""
import re

PLAN_RE = re.compile(r"^adreno8: MP_ASSIGN plan: batch (\d+) (table|data), permissions (.+?), "
                     r"argument page ([0-9a-fx]+), (\d+) regions$")
REGION_RE = re.compile(r"^adreno8: MP_ASSIGN plan: batch (\d+) region (\d+) addr ([0-9a-fx]+) "
                       r"len (\d+)$")
BEFORE_RE = re.compile(r"^adreno8: MP_ASSIGN batch (\d+) (table|data): (\d+) regions, (\d+) "
                       r"permission records, (\d+) top-level SCM calls before$")
AFTER_RE = re.compile(r"^adreno8: MP_ASSIGN batch (\d+) (table|data) returned (-?\d+), "
                      r"answer ([0-9a-fx]+), (\d+) top-level SCM calls "
                      r"\(A6 interrupted continuation SMCs not counted\)$")
SNAP_HEAD_RE = re.compile(r"^scmsnap call (\d+) argpage ([0-9a-fx]+) regions_bytes (\d+) "
                          r"sources ([0-9a-fx]+) sources0 (\d+) extended ([0-9a-fx]+) "
                          r"ext ([0-9a-fx]+) ([0-9a-fx]+) ([0-9a-fx]+) ([0-9a-fx]+)$")
SNAP_REGION_RE = re.compile(r"^scmsnap call (\d+) region (\d+) addr ([0-9a-fx]+) len (\d+)$")
SNAP_PERM_RE = re.compile(r"^scmsnap call (\d+) perm (\d+) vmid (\d+) perm (\d+) ctx (\d+) "
                          r"ctxsize (\d+) unused (\d+)$")

# Any line whose text begins with one of these prefixes is controlled: it must
# fully match one of the patterns above or the whole check fails. Lines with
# other driver logging pass through untouched.
CONTROLLED_PREFIXES = ("adreno8: MP_ASSIGN plan:", "adreno8: MP_ASSIGN batch ", "scmsnap ")
PATTERNS = (("plan", PLAN_RE), ("region", REGION_RE), ("before", BEFORE_RE),
            ("after", AFTER_RE), ("snap_head", SNAP_HEAD_RE),
            ("snap_region", SNAP_REGION_RE), ("snap_perm", SNAP_PERM_RE))

# C template both test fixtures insert into their fake SCM. It prints the wire
# records as seen at call entry, from the mapped argument page, to stdout: the
# same stream the production log uses, so the parser above sees the true
# interleaving. {call} is the fixture's current-call expression.
SNAP_C = '''
    /* Logcheck snapshot: wire records as this SCM entry sees them. */
    printf("scmsnap call %u argpage %#llx regions_bytes %llu sources %#llx sources0 %u "
        "extended %#llx ext %#llx %#llx %#llx %#llx\\n", (unsigned){call},
        (unsigned long long)argument0,
        (unsigned long long)argument1, (unsigned long long)argument2,
        (unsigned)logcheck_snap_sources(argument0),
        (unsigned long long)argument3,
        (unsigned long long)logcheck_snap_ext(argument0)[0],
        (unsigned long long)logcheck_snap_ext(argument0)[1],
        (unsigned long long)logcheck_snap_ext(argument0)[2],
        (unsigned long long)logcheck_snap_ext(argument0)[3]);
    for (unsigned i = 0; i < argument1 / 16; i++) {
        uint64_t *live = (uint64_t *)(uintptr_t)physical_table(argument0);
        printf("scmsnap call %u region %u addr %#llx len %llu\\n", (unsigned){call}, i,
            (unsigned long long)live[2 * i], (unsigned long long)live[2 * i + 1]);
    }
    for (unsigned i = 0; i < logcheck_snap_ext(argument0)[2] / 24; i++) {
        char *perm = (char *)(uintptr_t)physical_table(argument0) + 576
            + 24 * (unsigned long long)i;
        uint32_t vmid, bits, ctx_size, unused;
        uint64_t context;
        memcpy(&vmid, perm, 4);
        memcpy(&bits, perm + 4, 4);
        memcpy(&context, perm + 8, 8);
        memcpy(&ctx_size, perm + 16, 4);
        memcpy(&unused, perm + 20, 4);
        printf("scmsnap call %u perm %u vmid %u perm %u ctx %llu ctxsize %u unused %u\\n",
            (unsigned){call}, i, vmid, bits, (unsigned long long)context, ctx_size, unused);
    }
'''

SNAP_HELPERS_C = '''
static uint64_t *logcheck_snap_ext(uint64_t page_phys) {
    return (uint64_t *)((uintptr_t)physical_table(page_phys) + 640);
}
static uint32_t logcheck_snap_sources(uint64_t page_phys) {
    uint32_t first;
    memcpy(&first, (char *)(uintptr_t)physical_table(page_phys) + 512, 4);
    return first;
}
'''


def snap_c(call_expr):
    """Render the shared snapshot template for one fixture's call-index expression."""
    return SNAP_C.replace("{call}", call_expr)


# Production constants: VMID_HLOS 3, VMID_CP_PIXEL 0xA, PERM_READ 4, PERM_WRITE 2.
PERM_NOTE = {"table": "HLOS read/write and CP_PIXEL read", "data": "CP_PIXEL read/write"}
WIRE_PERM = {"table": [(3, 6), (10, 4)], "data": [(10, 6)]}


def _classify(line):
    for kind, pattern in PATTERNS:
        match = pattern.match(line)
        if match:
            return kind, match
    return None, None


def check(out, page_size=None, roles=None):
    """Single pass over the stream; returns (batches, calls, failed_batch_or_None).

    Order enforced line by line: plan -> exactly `count` consecutively indexed
    region lines -> before -> one or more complete snapshot groups (head, then
    all region records, then all permission records) -> after. A group must
    finish before the next head or the after line; the after line closes the
    batch. After a failed batch no further controlled line is accepted. When
    `roles` is given, batch roles must equal the caller-supplied sequence, so
    the table-before-data ordering is asserted from the scenario, not from
    the log itself.
    """
    batches, calls, failed = {}, {}, None
    cur = None                       # batch id currently open, else None
    phase = None                     # 'regions' | 'snaps' while cur is open
    next_region = 0                  # expected index of the next log region line
    done_groups = 0                  # completed snapshot groups, stream-wide
    open_call = None                 # snapshot group currently filling
    snap_phase, snap_regions, snap_perms = None, 0, 0
    for line in out.splitlines():
        if not line.startswith(CONTROLLED_PREFIXES):
            continue
        kind, m = _classify(line)
        assert kind is not None, f"unrecognized controlled line: {line}"
        assert failed is None, f"{kind} event for batch {m.group(1)} after failed batch {failed}"
        if kind == "plan":
            assert cur is None, f"plan for batch {m.group(1)} while batch {cur} is open"
            batch = int(m.group(1))
            assert batch == len(batches), f"batch numbers must ascend from 0: got {batch}"
            role = m.group(2)
            assert m.group(3) == PERM_NOTE[role], line
            argpage = int(m.group(4), 16)
            if page_size is not None:
                assert argpage % page_size == 0, line
            batches[batch] = {"role": role, "perm_note": m.group(3), "argpage": argpage,
                              "count": int(m.group(5)), "regions": [], "snaps": [],
                              "failed": False}
            cur, phase, next_region = batch, "regions", 0
        elif kind == "region":
            assert cur is not None and phase == "regions", f"region line outside its plan: {line}"
            assert int(m.group(1)) == cur, f"region line for batch {m.group(1)} in batch {cur}"
            batch = batches[cur]
            index = int(m.group(2))
            assert index == next_region and next_region < batch["count"], \
                f"batch {cur}: region index {index} breaks dense 0..{batch['count'] - 1}"
            length = int(m.group(4))
            if page_size is not None:
                assert length % page_size == 0, (cur, length)
            batch["regions"].append((m.group(3), length))
            next_region += 1
        elif kind == "before":
            assert cur is not None and phase == "regions", \
                f"before line outside its region block: {line}"
            assert int(m.group(1)) == cur, f"before line for batch {m.group(1)} in batch {cur}"
            batch = batches[cur]
            assert next_region == batch["count"], \
                f"batch {cur}: before after only {next_region} of {batch['count']} regions"
            assert m.group(2) == batch["role"], line
            assert int(m.group(3)) == batch["count"] and \
                int(m.group(4)) == len(WIRE_PERM[batch["role"]]), line
            assert int(m.group(5)) == done_groups, \
                f"batch {cur}: before says {m.group(5)} calls, {done_groups} completed"
            batch["before"] = {"regions": int(m.group(3)), "perms": int(m.group(4)),
                               "calls": int(m.group(5))}
            phase = "snaps"
        elif kind == "snap_head":
            assert cur is not None and phase == "snaps" and open_call is None, \
                f"snapshot head outside an open batch with a finished group: {line}"
            index = int(m.group(1))
            assert index == done_groups, f"snapshot call {index} out of order at {done_groups}"
            batch = batches[cur]
            argpage = int(m.group(2), 16)
            ext = tuple(int(g, 16) for g in m.groups()[6:])
            assert argpage == batch["argpage"], f"call {index}: argument page differs from plan"
            assert int(m.group(3)) == batch["count"] * 16, f"call {index}: regions_bytes {m.group(3)}"
            assert int(m.group(4), 16) == argpage + 512 and int(m.group(5)) == 3, \
                f"call {index}: sources {m.group(4)} value {m.group(5)}"
            assert int(m.group(6), 16) == argpage + 640, f"call {index}: extended pointer"
            perm_bytes = len(WIRE_PERM[batch["role"]]) * 24
            assert ext == (4, argpage + 576, perm_bytes, 0), f"call {index}: ext {ext}"
            assert batch["before"]["perms"] * 24 == perm_bytes
            calls[index] = {"argpage": argpage, "regions_bytes": int(m.group(3)),
                            "regions": [], "perms": []}
            open_call, snap_phase, snap_regions, snap_perms = index, "regions", 0, 0
        elif kind == "snap_region":
            assert open_call is not None and snap_phase == "regions", \
                f"snapshot region record outside its region block: {line}"
            assert int(m.group(1)) == open_call, f"region record for call {m.group(1)}"
            index = int(m.group(2))
            batch = batches[cur]
            assert index == snap_regions and snap_regions < batch["count"], \
                f"call {open_call}: snapshot region index {index} breaks dense order"
            pair = (m.group(3), int(m.group(4)))
            assert pair == batch["regions"][index], \
                f"call {open_call}: region {index} wire {pair} differs from log"
            calls[open_call]["regions"].append(pair)
            snap_regions += 1
            if snap_regions == batch["count"]:
                snap_phase = "perms"
        elif kind == "snap_perm":
            assert open_call is not None and snap_phase == "perms", \
                f"snapshot permission record outside its permission block: {line}"
            assert int(m.group(1)) == open_call, f"permission record for call {m.group(1)}"
            batch = batches[cur]
            index = int(m.group(2))
            want = WIRE_PERM[batch["role"]]
            assert index == snap_perms and snap_perms < len(want), \
                f"call {open_call}: snapshot perm index {index} breaks dense order"
            record = (int(m.group(3)), int(m.group(4)), int(m.group(5)),
                      int(m.group(6)), int(m.group(7)))
            assert (record[0], record[1]) == want[index] and record[2:] == (0, 0, 0), \
                f"call {open_call}: perm {index} wire {record} differs from expected {want}"
            calls[open_call]["perms"].append(record)
            snap_perms += 1
            if snap_perms == len(want):
                batch["snaps"].append(open_call)
                done_groups += 1
                open_call, snap_phase = None, None
        else:  # after
            assert cur is not None and phase == "snaps" and open_call is None, \
                f"returned line without a finished snapshot group: {line}"
            assert int(m.group(1)) == cur, f"returned line for batch {m.group(1)} in batch {cur}"
            batch = batches[cur]
            assert m.group(2) == batch["role"], line
            assert len(batch["snaps"]) >= 1, f"batch {cur}: returned without any SCM call"
            assert int(m.group(5)) == done_groups and \
                int(m.group(5)) - batch["before"]["calls"] == len(batch["snaps"]), \
                f"batch {cur}: call accounting {m.group(5)} vs before " \
                f"{batch['before']['calls']} and {len(batch['snaps'])} groups"
            batch["after"] = {"status": int(m.group(3)), "answer": int(m.group(4), 16),
                              "calls": int(m.group(5))}
            batch["failed"] = batch["after"]["status"] != 0 or batch["after"]["answer"] != 0
            if batch["failed"]:
                failed = cur
            cur, phase = None, None
    assert open_call is None, "stream ended inside an unfinished snapshot group"
    assert cur is None, "stream ended with a batch before its returned line"
    if roles is not None:
        assert [batches[b]["role"] for b in sorted(batches)] == list(roles), \
            f"log roles differ from scenario roles {list(roles)}"
    return batches, calls, failed
