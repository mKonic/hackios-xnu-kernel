#!/usr/bin/env python3
"""Host interval test for the annibale payload window contract (review U1, map side).

Two scopes are checked separately, per the review ruling, and neither one
alone would catch a coordinated wrong edit:
  - MemoryMap scope: the XNU_Payload entry must exist exactly once, carry the
    independent values pinned below, and overlap no other entry of the table
    MemoryInitPei consumes (that file quick-sorts by address and halts the
    boot when adjacent entries overlap);
  - DTB static scope: 64-bit reg cells of the pinned embedded DTB -- the
    address space of static memory reservations; 32-bit cells are per-bus
    device space and out of scope -- must not overlap the window, and the
    trust_ui_vm_region bytes the window end is derived from must be unchanged.
TrustZone use, LK dynamic allocation and RamPartition protocol content are
not covered here and nothing is claimed about them.  The DTB walk follows
Devicetree E-PAPR token values and refuses a struct region whose FDT_END is
missing or truncated, with behaviour fixtures on in-memory copies (mid-NOP
equivalence, END refusal cases); the fixed file itself is never modified.

The endpoint, tail, magic and trust_ui bytes are this file's own constants,
never read from the sources under test, so shrinking R and adjusting
XnuLoader.h together cannot produce a wrong-but-self-consistent pass.

The Mu-Silicium root is optional argv[1] or $MU_SILICIUM_ROOT and defaults to
<kernel-root>/Mu-Silicium. Only the DTB scope needs the FdtBlob file under
that root; when it is absent the DTB checks are skipped with an explicit
note while the map, header, maintenance-copy and interval checks still run.
Usage: python3 tools/test-annibale-payload-contract.py [Mu-Silicium-root]"""

import hashlib
import os
import re
import struct
import sys

TOOLS = os.path.dirname(os.path.abspath(__file__))
KERNEL = os.path.dirname(TOOLS)
DEFAULT_MU = os.path.join(KERNEL, "Mu-Silicium")

# Independent window contract (payload memory contract, main-agent ruling):
# the container may not reach the panic-log tail, and the window must end
# exactly where the fixed DTB pins trust_ui_vm_region.
PAYLOAD_BASE = 0xF0000000
PAYLOAD_END = 0xF3800000
TAIL = 0x00100000
PAYLOAD_REGION = PAYLOAD_END - PAYLOAD_BASE      # 0x03800000
PAYLOAD_CONTAINER = PAYLOAD_REGION - TAIL        # 0x03700000
WIRE_MAGIC = 0x50554E58

MAP_REL = os.path.join("Platforms", "Xiaomi", "annibalePkg", "Library", "MemoryMapLib", "MemoryMapLib.c")
HEADER_REL = os.path.join("Silicon", "Silicium", "SiliciumPkg", "Library", "BootManagerLib", "XnuLoader.h")
COPY_REL = os.path.join(KERNEL, "uefi", "annibale", "files", MAP_REL)
HEADER_OVL = os.path.join(KERNEL, "uefi", "files", HEADER_REL)
DTB_REL = os.path.join("Platforms", "Xiaomi", "annibalePkg", "FdtBlob", "sm8750-xiaomi-annibale.dtb")
DTB_SHA256 = "b30f254e22e06a40298ae0310ac8741525e02ecf431fa224008fdf1f0585e0d6"
TRUST_UI_NODE = "trust_ui_vm_region@f3800000"
TRUST_UI_REG = bytes.fromhex("00000000f3800000" + "0000000005000000")

# Devicetree E-PAPR §5.4.1 token values, asserted as this file's own numbers
# (see contract_fdt_spec_token_numbers) and used by the walker below.
FDT_MAGIC = 0xD00DFEED
FDT_BEGIN_NODE, FDT_END_NODE, FDT_PROP, FDT_NOP, FDT_END = 1, 2, 3, 4, 9

ROW_RE = re.compile(r'\{"([A-Za-z0-9_]+)",\s*(0x[0-9A-Fa-f]+),\s*(0x[0-9A-Fa-f]+),'
                    r'\s*(\w+),\s*(\w+),\s*(\w+),\s*(\w+),\s*(\w+)\}')
PAYLOAD_ROW = ("XNU_Payload", PAYLOAD_BASE, PAYLOAD_REGION, "AddMem", "SYS_MEM", "SYS_MEM_CAP",
               "Reserv", "WRITE_BACK_XN")


def overlap(a, al, b, bl):
    return a < b + bl and b < a + al


def read_bytes(path, missing_hint=None):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as exc:
        sys.exit("%s unreadable%s: %s" % (path, (" (" + missing_hint + ")") if missing_hint else "", exc))


def map_rows(map_text):
    return [(n, int(a, 16), int(l, 16), h, rt, ra, mt, aa)
            for n, a, l, h, rt, ra, mt, aa in ROW_RE.findall(map_text)]


def check_map(map_text):
    """MemoryMap scope: the payload entry exists once with the pinned values,
    and no other entry overlaps the reserved window."""
    fails = []
    rows = map_rows(map_text)
    payload = [r for r in rows if r[0] == "XNU_Payload"]
    if len(payload) != 1:
        fails.append("XNU_Payload appears %d times in the table, want exactly 1" % len(payload))
    else:
        got = payload[0]
        want = PAYLOAD_ROW
        if got != want:
            fails.append("XNU_Payload entry is %s, want %s" % (got[1:], want[1:]))
        for other in rows:
            if other[0] != "XNU_Payload" and overlap(*got[1:3], *other[1:3]):
                fails.append("XNU_Payload overlaps %s [%x,%x)" % (other[0], other[1], other[1] + other[2]))
    for row in rows:
        if row[0] != "XNU_Payload" and overlap(PAYLOAD_BASE, PAYLOAD_REGION, row[1], row[2]):
            fails.append("independent window [%x,%x) overlaps %s [%x,%x)"
                         % (PAYLOAD_BASE, PAYLOAD_END, row[0], row[1], row[1] + row[2]))
    return fails, len(rows) - 1


def header_macro(header_text, name):
    m = re.search(r"#define\s+%s\s+(0[xX][0-9A-Fa-f]+)" % name, header_text)
    return int(m.group(1), 16) if m else None


def check_header(header_text):
    """Production header must carry the same independently pinned window, and
    the container must stop short of the tail."""
    fails = []
    got = {name: header_macro(header_text, name)
           for name in ("XNU_PAYLOAD_BASE", "XNU_PAYLOAD_REGION_SIZE", "XNU_PAYLOAD_MAX_SIZE", "XNU_PAYLOAD_MAGIC")}
    want = {"XNU_PAYLOAD_BASE": PAYLOAD_BASE, "XNU_PAYLOAD_REGION_SIZE": PAYLOAD_REGION,
            "XNU_PAYLOAD_MAX_SIZE": PAYLOAD_CONTAINER, "XNU_PAYLOAD_MAGIC": WIRE_MAGIC}
    for name, value in want.items():
        if got[name] != value:
            fails.append("%s is %s, want 0x%x" % (name, got[name], value))
    if got["XNU_PAYLOAD_BASE"] is not None and got["XNU_PAYLOAD_MAX_SIZE"] is not None:
        if PAYLOAD_BASE + PAYLOAD_CONTAINER + TAIL != PAYLOAD_END:
            fails.append("pinned tail arithmetic itself broke")
    return fails


def walk_dtb_regs(data):
    """Return ([(node-path, reg-bytes)], meta, error); only pairs of 64-bit
    cells (len divisible by 16) are interpreted as memory ranges. Tokens are
    E-PAPR values: 1 BEGIN_NODE, 2 END_NODE, 3 PROP, 4 NOP, 9 END. The walk is
    bounded by size_dt_struct and must reach FDT_END inside it, the presence
    check fdt_check_full performs, so a missing or truncated END is an error
    here and an unknown token never ends the walk silently. meta records the
    END token position and the first PROP position for the fixture edits."""
    magic, totalsize, off_struct, off_strings = struct.unpack(">4I", data[:16])
    size_strings, size_struct = struct.unpack(">2I", data[32:40])
    if magic != FDT_MAGIC:
        return [], {}, "bad FDT magic %08x" % magic
    if totalsize > len(data) or not off_struct < off_struct + size_struct <= totalsize \
            or not off_strings <= off_strings + size_strings <= totalsize:
        return [], {}, "header sizes inconsistent with a %d-byte file" % len(data)
    end_region, s, path, out = off_struct + size_struct, off_struct, [], []
    meta = {"end": None, "first_prop": None}
    while s + 4 <= end_region:
        tok = struct.unpack(">I", data[s:s + 4])[0]
        if tok == FDT_END:
            meta["end"] = s
            return out, meta, None
        if tok == FDT_NOP:
            s += 4
            continue
        if tok == FDT_BEGIN_NODE:
            e = data.index(b"\0", s + 4)
            name = data[s + 4:e].decode("utf8", "replace")
            s = (e + 4) & ~3
            path = (path + [name]) if name != "/" else path
            continue
        if tok == FDT_END_NODE:
            path = path[:-1] if path else path
            s += 4
            continue
        if tok == FDT_PROP:
            if meta["first_prop"] is None:
                meta["first_prop"] = s
            if s + 8 > end_region:
                return out, meta, "PROP header runs past the struct region"
            plen, noff = struct.unpack(">II", data[s + 4:s + 12])
            vstart = s + 12
            if vstart + plen > end_region or off_strings + noff >= totalsize:
                return out, meta, "PROP body or name runs past its region"
            ne = data.index(b"\0", off_strings + noff)
            pname = data[off_strings + noff:ne].decode("utf8", "replace")
            val = data[vstart:vstart + plen]
            s = (vstart + plen + 3) & ~3
            if pname == "reg":
                out.append(("/" + "/".join(path), val))
            continue
        return out, meta, "unknown token %08x at struct offset %x" % (tok, s)
    return out, meta, "struct region ends without FDT_END"


def check_dtb(dtb_bytes):
    fails = []
    digest = hashlib.sha256(dtb_bytes).hexdigest()
    if digest != DTB_SHA256:
        return ["fixed DTB is %s, not the pinned %s: static reservations may have moved, "
                "re-check the window against the new trust_ui reg and update both together"
                % (digest, DTB_SHA256)], 0
    regs, meta, err = walk_dtb_regs(dtb_bytes)
    if err:
        return ["FDT walk refused: %s" % err], 0
    trust = [v for p, v in regs if p.endswith(TRUST_UI_NODE) and len(v) == 16]
    if len(trust) != 1:
        fails.append("%s appears %d times with 16-byte reg, want exactly 1" % (TRUST_UI_NODE, len(trust)))
    elif trust[0] != TRUST_UI_REG:
        fails.append("%s reg is %s, want %s" % (TRUST_UI_NODE, trust[0].hex(), TRUST_UI_REG.hex()))
    checked = 0
    for path, val in regs:
        for i in range(0, len(val) - len(val) % 16, 16):
            addr, size = struct.unpack(">QQ", val[i:i + 16])
            if not size:
                continue
            checked += 1
            if overlap(PAYLOAD_BASE, PAYLOAD_REGION, addr, size):
                fails.append("DTB static reservation %s [%x,%x) overlaps window [%x,%x)"
                             % (path, addr, addr + size, PAYLOAD_BASE, PAYLOAD_END))
    return fails, checked


def insert_nop(data, at):
    """Byte copy of `data` with one FDT_NOP token inserted at `at` (inside the
    struct region), with totalsize and size_dt_struct grown and every header
    offset pointing at or behind `at` shifted, so the copy stays a
    well-formed DTB rather than an accidental corruption."""
    out = bytearray(data[:at] + struct.pack(">I", FDT_NOP) + data[at:])
    for field in (4, 36):                              # totalsize, size_dt_struct
        struct.pack_into(">I", out, field, struct.unpack_from(">I", out, field)[0] + 4)
    for field in (12, 16):                             # off_dt_strings, off_mem_rsvmap
        value = struct.unpack_from(">I", out, field)[0]
        if value >= at:
            struct.pack_into(">I", out, field, value + 4)
    return bytes(out)


def check_fdt_fixtures(dtb_bytes):
    """Behaviour fixtures on in-memory copies of the fixed DTB; the file
    itself and its hash pin keep the separate scope in check_dtb. A walker
    that mixed up NOP and END cannot pass all four."""
    fails = []
    regs, meta, err = walk_dtb_regs(dtb_bytes)
    if err or meta["end"] is None:
        return ["END stop refused on the unparsed file: %s" % err]
    struct_size = struct.unpack_from(">I", dtb_bytes, 36)[0]
    struct_end = struct.unpack_from(">I", dtb_bytes, 8)[0] + struct_size
    if meta["end"] + 4 != struct_end:
        return ["FDT_END is not the last struct token (%+d bytes after it), "
                "the END fixtures below would not mean what they claim" % (struct_end - meta["end"] - 4)]
    mid = meta["first_prop"] if meta["first_prop"] is not None else meta["end"]

    nop_regs, _, nop_err = walk_dtb_regs(insert_nop(dtb_bytes, mid))
    if nop_err:
        fails.append("mid-NOP fixture refused: %s" % nop_err)
    elif nop_regs != regs:
        fails.append("mid-NOP fixture changed the reg set: the walk stopped at the NOP "
                     "instead of continuing to the reservations behind it")
    elif not any(p.endswith(TRUST_UI_NODE) for p, _ in nop_regs):
        fails.append("mid-NOP fixture lost %s" % TRUST_UI_NODE)

    no_end = bytearray(dtb_bytes)                      # END token replaced by 0
    struct.pack_into(">I", no_end, meta["end"], 0)
    if walk_dtb_regs(bytes(no_end))[2] is None:
        fails.append("missing-END fixture was not refused")

    truncated = bytearray(dtb_bytes)                   # struct region shrinks past END
    struct.pack_into(">I", truncated, 36, struct_size - 4)
    err = walk_dtb_regs(bytes(truncated))[2]
    if err is None or "FDT_END" not in err:
        fails.append("truncated-END fixture was not refused (got %r)" % err)
    return fails


def main():
    mu = os.path.abspath(sys.argv[1]) if len(sys.argv) == 2 and sys.argv[1] not in ("-h", "--help") \
        else os.path.abspath(os.environ.get("MU_SILICIUM_ROOT", DEFAULT_MU))
    if len(sys.argv) > 2:
        sys.exit("usage: test-annibale-payload-contract.py [Mu-Silicium-root]")

    fails = []

    def ck(cond, name, extra=""):
        if not cond:
            fails.append("FAIL contract_%s%s" % (name, ("\n" + extra) if extra else ""))

    ck((FDT_BEGIN_NODE, FDT_END_NODE, FDT_PROP, FDT_NOP, FDT_END, FDT_MAGIC)
       == (1, 2, 3, 4, 9, 0xD00DFEED), "fdt_spec_token_numbers")

    mu_map = read_bytes(os.path.join(mu, MAP_REL), "pass the Mu-Silicium root as argv[1] or set MU_SILICIUM_ROOT")
    mu_header = read_bytes(os.path.join(mu, HEADER_REL))

    # byte-equality: the maintenance copy and the overlay header are the
    # kernel-side inputs the README documents against this Mu tree
    copy_bytes = read_bytes(COPY_REL, "the kernel maintenance copy under uefi/annibale/files is missing")
    ck(copy_bytes == mu_map, "maintenance_map_copy_matches_Mu")
    ck(read_bytes(HEADER_OVL) == mu_header, "overlay_header_matches_Mu")

    # scope 1: MemoryMap table
    entry = check_map(mu_map.decode())
    mf, others = entry
    for why in mf:
        fails.append("FAIL map_%s" % why)
    print("map scope: %d other MemoryMap entries checked against window [%x,%x)"
          % (others, PAYLOAD_BASE, PAYLOAD_END))

    # scope 2: production header arithmetic (compared against constants above)
    for why in check_header(mu_header.decode()):
        fails.append("FAIL header_%s" % why)

    # DTB static scope: skipped explicitly when the file is absent
    dtb_path = os.path.join(mu, DTB_REL)
    if os.path.exists(dtb_path):
        dtb_bytes = read_bytes(dtb_path)
        df, checked = check_dtb(dtb_bytes)
        for why in df:
            fails.append("FAIL dtb_%s" % why)
        for why in check_fdt_fixtures(dtb_bytes):
            fails.append("FAIL fdt_fixture_%s" % why)
        print("dtb scope: %d nonzero 64-bit reg cells checked against window [%x,%x); "
              "32-bit device-space cells, TrustZone use and dynamic reservations are out of scope"
              % (checked, PAYLOAD_BASE, PAYLOAD_END))
        print("fdt walker fixtures: mid-NOP equivalence, END-as-last-token, missing-END and truncated-END refusals")
    else:
        print("SKIP dtb scope (%s absent: hash pin, trust_ui reg and reservation overlap not checked)" % dtb_path)

    # mutations: each wrong edit must produce at least one failing check,
    # so passing checks above are discriminating and not vacuous
    def must_fail(fn, text, name):
        got = fn(text)
        got = got[0] if isinstance(got, tuple) else got
        if not got:
            fails.append("FAIL mutation_%s_not_caught" % name)

    m = mu_map.decode()
    must_fail(check_map, m.replace("0xF0000000, 0x03800000", "0xF0000000, 0x04000000"),
              "map_enlarged_to_64MiB")
    must_fail(check_map, "\n".join(l for l in m.splitlines() if "XNU_Payload" not in l),
              "map_entry_removed")
    must_fail(check_map, m.replace("0x03800000, AddMem, SYS_MEM, SYS_MEM_CAP, Reserv",
                                   "0x03800000, AddMem, SYS_MEM, SYS_MEM_CAP, Conv"),
              "map_type_conventional")
    h = mu_header.decode()
    must_fail(check_header, h.replace("0x03800000ULL", "0x04000000ULL"), "header_region_64MiB")
    must_fail(check_header, h.replace("0x03700000ULL", "0x03800000ULL"), "header_container_reaches_end")
    must_fail(check_header, h.replace("0x50554E58", "0x50554E59"), "header_magic_retargeted")

    if fails:
        print("\n".join(fails))
        sys.exit(1)
    print("annibale-payload-contract: map scope, header arithmetic, maintenance-copy equality, "
          "six mutation counterexamples"
          + (", FDT walker fixtures" if os.path.exists(dtb_path) else " (dtb scope skipped)")
          + " passed")


if __name__ == "__main__":
    main()
