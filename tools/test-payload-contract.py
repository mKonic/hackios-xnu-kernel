#!/usr/bin/env python3
"""Host test for the loader's payload memory contract (review U1).

The contract constants, the two structs, parse_number/str_eq/starts_with, the whole
parse_payload block and the `download:` branch are extracted verbatim from
xnuloader/loader.c and compiled against a 56 MiB RAM array standing in for the
payload region: declared-size bound C, per-entry bounds, NUL rules, the
actual-received-bytes gate that keeps a short or cancelled download from leaving a
consumable-looking header, the pre-clear of the header when a new normal download
starts, and the untouched ramdisk path. Constants are checked against the contract
sheet numbers inside this test. Mutations delete each guard and must fail scenarios.
Usage: python3 tools/test-payload-contract.py"""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOADER = os.path.join(ROOT, "xnuloader", "loader.c")
BUILD = os.environ.get("TMPDIR", "/tmp")

B = 0xF0000000
R = 0x03800000
C = 0x03700000
PANIC_BASE = 0xF3700000
PANIC_SIZE = 0x80000


def read(p):
    with open(p) as f:
        return f.read()


def read_tu(path):
    """xnuloader/loader.c as the compiler sees it: its own fragments inlined in include order."""
    src = read(path)
    base = os.path.dirname(path)
    return re.sub(r'^#include "(loader_[A-Za-z0-9_]+\.inc)"\n',
                  lambda m: read(os.path.join(base, m.group(1))), src, flags=re.M)


def macro(src, name):
    m = re.search(r"^#define\s+%s\s+(\S+)" % name, src, re.M)
    if not m:
        sys.exit("macro %s not found in loader.c" % name)
    tok = m.group(1)
    return int(tok.rstrip("ULLull"), 16) if tok.startswith("0x") else int(tok)


def span(src, start, end, name):
    i = src.index(start)
    j = src.index(end, i) + len(end)
    return src[i:j] if name else None, i, j


def block(src, start, end, label):
    try:
        i = src.index(start)
        j = src.index(end, i) + len(end)
    except ValueError:
        sys.exit("anchor for %s not found in loader.c: update the test" % label)
    return src[i:j]


def main():
    src = read_tu(LOADER)
    if (macro(src, "PAYLOAD_RESERVE_SIZE"), macro(src, "PAYLOAD_CONTAINER_MAX"), macro(src, "PANIC_LOG_BASE"),
            macro(src, "PANIC_LOG_SIZE")) != (R, C, PANIC_BASE, PANIC_SIZE):
        sys.exit("contract constants changed: %x %x %x %x" %
                 (macro(src, "PAYLOAD_RESERVE_SIZE"), macro(src, "PAYLOAD_CONTAINER_MAX"),
                  macro(src, "PANIC_LOG_BASE"), macro(src, "PANIC_LOG_SIZE")))
    print("loader.c keeps the contract constants R=%x C=%x panic=%x/%x: PASS" % (R, C, PANIC_BASE, PANIC_SIZE))
    if "PAYLOAD_MAX_SIZE" in src:
        sys.exit("the single old MAX name survived: two named bounds are required")
    if "PAYLOAD_BASE + PAYLOAD_RESERVE_SIZE" not in src or "PAYLOAD_CONTAINER_MAX;" not in src:
        sys.exit("choose_window or the download bound does not use the two named bounds")

    pinfo = block(src, "struct payload_info {", "\n};\n", "payload_info")
    structs = block(src, "struct payload_header {", "struct payload_entry { char name[16]; uint32_t size; uint32_t reserved; };", "structs")
    helpers = (block(src, "static int\nstr_eq(", "\n}\n", "str_eq")
               + block(src, "static uint64_t\nparse_number(", "\n}\n", "parse_number"))
    starts = block(src, "static int\nstarts_with(", "\n}\n", "starts_with")
    parse = block(src, "static uint64_t payload_bytes;", "\treturn 1;\n}", "parse_payload")
    dl = block(src, '} else if (starts_with(cmd, "download:")) {', '} else if (starts_with(cmd, "upload"))', "download branch")
    dl_body = dl[len('} else if (starts_with(cmd, "download:")) {'):-len('} else if (starts_with(cmd, "upload"))')]

    macros = "\n".join(line for line in src.splitlines() if re.match(
        r"#define (PAYLOAD_MAGIC|PAYLOAD_CONTAINER_MAX|PAYLOAD_RESERVE_SIZE|RESIDENT_RD_OFFSET|RESIDENT_RD_RESERVE|USB_CHUNK)\b", line))
    prelude = """#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define print(...) printf(__VA_ARGS__)
static uint8_t ram[0x03800000];   /* the reserved region, addressed like the loader does */
#define PAYLOAD_BASE ((uint64_t)(uintptr_t)ram)
""" + macros + """
static char last_reply[96];
static void fb_reply(const char *a, const char *b) { snprintf(last_reply, sizeof(last_reply), "%s %s", a, b); }
static void fb_reply_data(uint64_t n) { snprintf(last_reply, sizeof(last_reply), "DATA %llx", (unsigned long long)n); }
static uint64_t win_size, win_base, resident_rd_size;
static int download_to_ramdisk;
static uint8_t usb_rx_buf[USB_CHUNK];
static const uint8_t *rx_src;
static uint64_t rx_left, rx_chunk;
static int64_t usb_rx(uint8_t *buf, uint64_t len)
{
	uint64_t n = len < rx_left ? len : rx_left;
	if (n > rx_chunk) {
		n = rx_chunk;
	}
	if (n == 0) {
		return -1;
	}
	memcpy(buf, rx_src, (size_t)n);
	rx_src += n;
	rx_left -= n;
	return (int64_t)n;
}
"""

    driver = """static const uint8_t *img;   /* scripted download content, never freed */
static int checks_failed;

static void ck(int ok, const char *name)
{
	if (!ok) {
		checks_failed++;
		printf("FAIL %s\\n", name);
	}
}

static void set_download(const uint8_t *bytes, uint64_t left, uint64_t chunk)
{
	img = bytes;
	rx_src = bytes;
	rx_left = left;
	rx_chunk = chunk;
}

static uint32_t rd32(const uint8_t *p) { return (uint32_t)p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }

static int scen(void)
{
	struct payload_info pi;
"""
    scenarios_c = []
    mutations = []

    def entry(name, data):
        n = name if isinstance(name, bytes) else name.encode()
        n = n + b"\0" * (16 - len(n))
        pad = b"\0" * ((-len(data)) & 15)
        return n + len(data).to_bytes(4, "little") + b"\0\0\0\0" + data + pad

    def emit(name, body):
        scenarios_c.append("	/* %s */\n%s" % (name, body))

    img_no = [0]

    def image_decl(tag, image):
        """Small images: an initializer list. Large ones (the 55 MiB container probes): a BSS
        array built at run time from runs -- the same exact bytes and boundary touches,
        minutes of compiler work on decimal initializer lists dropped."""
        if len(image) <= 4096:
            return "\t\tstatic const uint8_t %s[] = {%s};\n" % (tag, ",".join(str(b) for b in image))
        out = "\t\tstatic uint8_t %s[%d];\n" % (tag, len(image))
        i, start = 0, 0
        while i < len(image):
            v = image[i]
            j = i
            while j < len(image) and image[j] == v:
                j += 1
            if j - i >= 64:
                if i > start:
                    out += "\t\tmemcpy(%s + %d, (const uint8_t[]){%s}, %d);\n" % (
                        tag, start, ",".join(str(b) for b in image[start:i]), i - start)
                out += "\t\tmemset(%s + %d, %d, %d);\n" % (tag, i, v, j - i)
                i = start = j
            else:
                i = j
        if start < len(image):
            out += "\t\tmemcpy(%s + %d, (const uint8_t[]){%s}, %d);\n" % (
                tag, start, ",".join(str(b) for b in image[start:]), len(image) - start)
        return out

    def scenario_c(name, image, payload_bytes, expect_parse, extra=""):
        img_no[0] += 1
        tag = "im%d" % img_no[0]
        body = """	{
%s
		memset(ram, 0, sizeof(ram));
		memcpy(ram, %s, sizeof(%s));
		payload_bytes = %dULL;
		int r = parse_payload(&pi, 0);
		ck(r == %d, "%s");
%s	}
""" % (image_decl(tag, image), tag, tag, payload_bytes, expect_parse, name, extra)
        emit(name, body)

    TERM = b"\0" * 24
    body = entry(b"kernel", b"K" * 0x20) + entry(b"cmdline", b"x=1\0") + TERM
    image = b"XNUP" + (len(body)).to_bytes(4, "little") + body
    scenario_c("parse_valid", image, 0, 1, """		ck(pi.kernel == ram + 8 + 24 && pi.cmdline && pi.cmdline[0] == 'x', "parse_valid_fields");
""")
    # exact container bound: two padded entries plus the terminator land the last byte on C
    y, x = 0x1000, C - 80 - 0x1000
    assert x % 16 == 0 and x > 0
    body_ok = entry(b"kernel", b"K" * x) + entry(b"fill", b"z" * y) + TERM
    image_ok = b"XNUP" + (len(body_ok)).to_bytes(4, "little") + body_ok
    assert len(image_ok) == C
    scenario_c("parse_at_c", image_ok, 0, 1)
    image_c1 = b"XNUP" + (len(body_ok) + 1).to_bytes(4, "little") + body_ok
    scenario_c("parse_past_c", image_c1, 0, 0)
    body_over = entry(b"kernel", b"K" * (0x1000 - 8))   # pad 0x1000: entry needs 0x1018
    image_over = b"XNUP" + (0x1000).to_bytes(4, "little") + body_over  # declared body one 0x18 short
    scenario_c("parse_entry_over_runs", image_over, 0, 0)
    scenario_c("parse_entry_exact", image, 0, 1)
    body_unam = b"A" * 16 + (0).to_bytes(4, "little") + b"\0\0\0\0" + TERM
    scenario_c("parse_name_without_nul", b"XNUP" + len(body_unam).to_bytes(4, "little") + body_unam, 0, 0)
    body_tx = entry(b"cmdline", b"abcd") + TERM
    scenario_c("parse_text_without_nul", b"XNUP" + len(body_tx).to_bytes(4, "little") + body_tx, 0, 0)
    f = len(image)
    scenario_c("gate_short", image, f - 1, 0)
    scenario_c("gate_full", image, f, 1)
    scenario_c("gate_extra", image, f + 1, 0)   # a successful download longer than the declared file
    scenario_c("gate_zero_preload", image, 0, 1)
    # the end must be proven by a full all-zero record ending the body exactly (independent repro)
    body_nt = body[:-len(TERM)]
    scenario_c("term_missing", b"XNUP" + len(body_nt).to_bytes(4, "little") + body_nt, 0, 0,
               """		ck(pi.kernel == NULL, "term_missing_no_kernel_kept");
""")
    tail1 = body + b"\x01"
    scenario_c("term_one_byte_tail", b"XNUP" + len(tail1).to_bytes(4, "little") + tail1, 0, 0)
    bad_term = b"\0" * 16 + (0).to_bytes(4, "little") + (7).to_bytes(4, "little")
    body_nz = body[:-len(TERM)] + bad_term
    scenario_c("term_nonzero_inside", b"XNUP" + len(body_nz).to_bytes(4, "little") + body_nz, 0, 0)
    body_ex = body + b"\x11"
    scenario_c("term_then_extra_byte", b"XNUP" + len(body_ex).to_bytes(4, "little") + body_ex, 0, 0)
    scenario_c("term_only_container", b"XNUP" + (24).to_bytes(4, "little") + TERM, 0, 1)

    def hx(data):
        return ",".join(str(b) for b in data)

    dl_scen = """	{
		static const uint8_t imdl[] = {@IMDL@};
		static const uint8_t imdlx[] = {@IMDLX@};
		static const uint8_t imdl512[0x200] = {@IMDL512@};
		uint64_t total = sizeof(imdl);
		char cmd[64];
		snprintf(cmd, sizeof(cmd), "download:%x", (unsigned)total);

		/* full transfer: bytes complete the header and the record accepts the parse */
		memset(ram, 0, sizeof(ram));
		memcpy(ram, imdl, sizeof(imdl));
		set_download(imdl, total, 0x10000);
		last_reply[0] = 0;
		h_download(cmd);
		ck(str_eq(last_reply, "OKAY ") && payload_bytes == total, "dl_full_okay");
		ck(parse_payload(&pi, 0) == 1 && pi.kernel != NULL, "dl_full_parses");

		/* short transfer: the record keeps the real count and the half-written header
		 * is erased, so nothing of the stop-half container stays consumable */
		memcpy(ram, imdl, sizeof(imdl));
		set_download(imdl, total / 2, 0x10000);
		h_download(cmd);
		ck(str_eq(last_reply, "FAIL short download") && payload_bytes == total / 2, "dl_short_recorded");
		ck(rd32(ram) != 0x50554E58u && parse_payload(&pi, 0) == 0 && pi.kernel == NULL,
		   "dl_short_publishes_nothing");

		/* cancelled after the whole container arrived (independent repro: a complete-
		 * looking prefix of a longer request must still not be published) */
		memcpy(ram, imdl, sizeof(imdl));
		snprintf(cmd, sizeof(cmd), "download:%x", (unsigned)(total + 16));
		set_download(imdl, total, 0x10000);
		h_download(cmd);
		ck(str_eq(last_reply, "FAIL short download") && payload_bytes == total, "dl_cancel_prefix_recorded");
		ck(rd32(ram) != 0x50554E58u, "dl_cancel_prefix_header_cleared");
		ck(parse_payload(&pi, 0) == 0 && pi.kernel == NULL, "dl_cancel_prefix_not_consumable");

		/* a successful transfer carrying bytes past the declared file: recorded, but
		 * only the exact record may match the declared length */
		memcpy(ram, imdlx, sizeof(imdlx));
		snprintf(cmd, sizeof(cmd), "download:%x", (unsigned)(total + 1));
		set_download(imdlx, total + 1, 0x10000);
		h_download(cmd);
		ck(str_eq(last_reply, "OKAY ") && payload_bytes == total + 1, "dl_long_okay");
		ck(parse_payload(&pi, 0) == 0, "dl_long_not_consumable");

		/* a download that delivers nothing still invalidates the previous container */
		memcpy(ram, imdl, sizeof(imdl));
		snprintf(cmd, sizeof(cmd), "download:%x", (unsigned)total);
		set_download(imdl, 0, 0);
		h_download(cmd);
		ck(rd32(ram) != 0x50554E58u && payload_bytes == 0, "dl_empty_clears_header");
		ck(parse_payload(&pi, 0) == 0, "dl_empty_not_consumable");

		/* bad size refuses without touching the resident container */
		memcpy(ram, imdl, sizeof(imdl));
		payload_bytes = total;
		snprintf(cmd, sizeof(cmd), "download:%x", 0x03700001);
		set_download(imdl, 0, 0);
		h_download(cmd);
		ck(str_eq(last_reply, "FAIL bad size") && payload_bytes == total && parse_payload(&pi, 0) == 1,
		   "dl_bad_size_keeps_container");

		/* exactly C is the accepted bound for a normal download */
		{
			static uint8_t bigimg[@C@];
			char big[64];
			snprintf(big, sizeof(big), "download:%x", @C@);
			memset(bigimg, 0x5A, sizeof(bigimg));
			set_download(bigimg, @C@ULL, 0x100000);
			h_download(big);
			ck(str_eq(last_reply, "OKAY ") && payload_bytes == @C@ULL, "dl_at_c_okay");
		}

		/* ramdisk mode keeps its own window bound and never touches the payload record */
		payload_bytes = 0;
		download_to_ramdisk = 1;
		win_base = (uint64_t)(uintptr_t)ram - RESIDENT_RD_OFFSET;
		win_size = RESIDENT_RD_OFFSET + RESIDENT_RD_RESERVE + 0x400;
		snprintf(cmd, sizeof(cmd), "download:%x", 0x200);
		set_download(imdl512, 0x200, 0x10000);
		h_download(cmd);
		ck(str_eq(last_reply, "OKAY ") && resident_rd_size == 0x200 && payload_bytes == 0, "dl_ramdisk_independent");
	}
""" .replace("@IMDL@", hx(image)).replace("@IMDLX@", hx(image + b"\x11")) \
     .replace("@IMDL512@", hx(image + b"\0" * (0x200 - len(image)))).replace("@C@", str(C))
    emit("download", dl_scen)

    tail = """
	printf(checks_failed ? "payload contract: %d checks failed\\n" : "payload contract: all checks passed\\n", checks_failed);
	return checks_failed ? 1 : 0;
}

int
main(void)
{
	return scen();
}
"""

    c_text = (prelude + structs + "\n" + pinfo + "\n" + helpers + "\n" + starts + "\n"
              + parse + "\nstatic void\nh_download(const char *cmd)\n{\n" + dl_body + "\n}\n"
              + driver + "\n".join(scenarios_c) + tail)

    def compile_run(text, name):
        cpath = os.path.join(BUILD, "payload-contract-%s.c" % name)
        xpath = os.path.join(BUILD, "payload-contract-%s" % name)
        with open(cpath, "w") as fobj:
            fobj.write(text)
        cmd = ["cc", "-std=gnu11", "-O0"]
        if os.environ.get("PC_ASAN"):
            cmd.append("-fsanitize=address")
        r = subprocess.run(cmd + ["-o", xpath, cpath], capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit("compile of %s failed:\n%s" % (name, r.stderr))
        r = subprocess.run([xpath], capture_output=True, text=True)
        return r.returncode, r.stdout

    rc, out = compile_run(c_text, "prod")
    print(out.rstrip())
    if rc != 0:
        sys.exit("production parse/download build failed")

    for name, old, new in (
            ("gate_removed", "(payload_bytes != 0 && payload_bytes != sizeof(struct payload_header) + ph->size)", "0"),
            ("gate_lenient", "payload_bytes != sizeof(struct payload_header) + ph->size",
             "payload_bytes < sizeof(struct payload_header) + ph->size"),
            ("pad_unaligned", "uint64_t pad = ((uint64_t)e->size + 15) & ~15ull;",
             "uint64_t pad = (uint64_t)e->size;"),
            ("name_nul_dropped", "|| !nul_within(e->name, sizeof(e->name))", ""),
            ("text_nul_dropped", "\t\t\tif (!nul_within(text, e->size)) {\n\t\t\t\tbad = 1;\n\t\t\t\tbreak;\n\t\t\t}\n", ""),
            ("postclear_dropped", "\t\t\t\t\tph->magic = 0;\n\t\t\t\t\tph->size = 0;\n", ""),
            ("term_end_dropped", "ended = p + sizeof(*e) == pend;", "ended = 1;"),
            ("term_zero_dropped", "\t\t\t\tif (raw[z] != 0) {\n\t\t\t\t\tended = 0;\n\t\t\t\t}\n", ""),
            ("term_required_dropped", "\tif (!bad && !ended) {\n\t\tbad = 1;   /* the entries ran to the body end with no terminator record */\n\t}\n", ""),
    ):
        if c_text.count(old) < 1:
            sys.exit("mutation %s: extracted text changed, update the test" % name)
        patched = c_text.replace(old, new)
        rc, mout = compile_run(patched, name)
        lines = [l for l in mout.splitlines() if l.startswith("FAIL")]
        if rc == 0:
            sys.exit("mutation %s survived: the tests miss it" % name)
        print("mutation check: %-16s caught by %d checks (%s...)" % (name, len(lines), lines[0]))
    print("payload contract tests passed; the loader translation unit is %d lines" % (src.count(chr(10)) + 1))


if __name__ == "__main__":
    main()
