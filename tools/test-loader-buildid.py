#!/usr/bin/env python3
"""Host test for the loader build identifier (fastboot getvar build-id).

The LC-STR, LC-FB, LC-USB and LC-BID blocks of xnuloader/loader.c are extracted verbatim
(the extraction helpers come from tools/test-loader-lifecycle.py) and run through the real
fb_reply packer: fb_getvar_identity("build-id") must answer OKAY plus the built-in
identifier, product and version must answer exactly as before, and any other variable must
be left for the caller. Two host builds prove the two states of the identifier: the plain
one must report the default "unknown", one built with -DLOADER_BUILD_ID="<40 hex>" (the
shape build.sh accepts: a full source commit) must report that value unchanged. The build.sh
checks run against a throwaway copy of the script, never the production directory: mock
clang and lld-link record every argv element and write the outputs themselves, and a
sentinel loader.obj stands in for an earlier build. A valid 40-hex BUILD_ID must reach the
compiler as one -DLOADER_BUILD_ID argument that keeps the C string quotes, unset must warn
and pass no flag, and an empty or malformed value must exit non-zero with zero tool calls
and the sentinel untouched. The captured valid argument then compiles a const char *
consumer with the real C compiler. Both whole-loader
-fsyntax-only variants run as well. The identifier is what the image claims to be: the
manifest and the load record back what actually ran; no claim here is a cryptographic proof.
Usage: python3 tools/test-loader-buildid.py"""

import importlib.util
import os
import re
import shutil
import subprocess
import sys

TOOLS = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("loader_lifecycle_base", os.path.join(TOOLS, "test-loader-lifecycle.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)   # the lifecycle driver keeps its work behind __main__, import is quiet

PRELUDE = _base.PRELUDE
LOADER = _base.LOADER
EFIH = _base.EFIH
BUILD = _base.BUILD
BID = "0123456789abcdef0123456789abcdef01234567"   # the shape build.sh accepts: 40 lower-case hex
BID_SCENARIOS = r"""/* ---- build-id scenarios ---- */
static void bid_clean(void)
{
	USB = &h_usb; usb_connected = kernel_resources_held = 1;
	firmware_fault = 0;
	running_live = NULL;
	usb_rx_queued = usb_out_owned = usb_in_owned = usb_out_lost = usb_in_lost = 0;
	ev_calls = ev_sends = ev_after = ev_change = ev_change_state = 0;
	ev_msg = UsbDeviceEventNoEvent; ev_step = clk_freq / 10; clk_now = 0;
	ev_err = 0; ev_status = -1; ev_size = 0; ev_size_set = 0; ev_no_events = 0; ev_buf = NULL;
	ev_out_buf = ev_in_buf = NULL;
	usb_out_buf = NULL; usb_out_len = 0; usb_in_buf = NULL; usb_in_len = 0;
	memset(usb_tx_buf, 0, 64);   /* the reply ends where the packer stops writing */
}

static void bid_expect(const char *want)
{
	CK(ev_sends == 1, "bid_sent_once");
	CK(str_eq((const char *)usb_tx_buf, want), "bid_reply_text");
}

int main(void)
{
	CK(fb_getvar_identity("@BID@_never_a_var") == 0, "bid_foreign_var_unclaimed");
	CK(ev_sends == 0, "bid_foreign_var_no_reply");
	bid_clean();
	CK(fb_getvar_identity("build-id") == 1, "bid_answered");
	bid_expect("OKAY@BID@");
	bid_clean();
	CK(fb_getvar_identity("product") == 1, "bid_product_answered");
	bid_expect("OKAYxnuloader");
	bid_clean();
	CK(fb_getvar_identity("version") == 1, "bid_version_answered");
	bid_expect("OKAY0.5");
	if (fail_count == 0) { printf("build-id checks passed\n"); } else { printf("%d failures\n", fail_count); }
	return fail_count > 127 ? 127 : fail_count;
}
"""


def build_c(src, bid_expect):
    # only the blocks the identity answers reach: str_eq, the lifecycle core LC-USB leans on,
    # the ownership code behind usb_tx, the reply packer and the identifier itself
    blocks = [_base.extract(src, name) for name in ("LC-STR", "LC", "LC-USB", "LC-FB", "LC-BID")]
    prelude = PRELUDE
    exc = re.search(r"enum \{ EXC_CTX_.*?\};", _base.read(EFIH), re.S).group(0)
    usb = re.search(r"enum \{ UsbDeviceEventNoEvent.*?\} usb_device_event_data;", _base.read(EFIH), re.S).group(0)
    for token, value in (("@C_PRELUDE@", _base._harness3.C_PRELUDE),
                         ("@START@", _base.macro_value(src, "START_SECONDS")), ("@SILENCE@", _base.macro_value(src, "SILENCE_SECONDS")),
                         ("@SELFTEST@", _base.macro_value(src, "CHECKPOINT_SELFTEST")), ("@MAGIC2@", _base.macro_value(src, "KERNEL_LIVE_MAGIC2")),
                         ("@LIVE@", _base.struct_copy(src, "kernel_live")), ("@EXC@", exc), ("@USB@", usb),
                         ("@DESCSET@", re.search(r"typedef struct \{[^}]*\} usb_device_descriptor_set;",
                                                 _base.read(EFIH), re.S).group(0)),
                         ("@PSCI@", _base._harness2.PSCI_STUB)):
        prelude = prelude.replace(token, value)
    return (prelude + "\n" + "\n".join(blocks) + "\n" + BID_SCENARIOS + "\n").replace("@BID@", bid_expect)


MOCK_TOOL = r"""#!/bin/bash
log=${ARGVLOG:?}
printf '%s\0' "$(basename "$0")" >>"$log"
for a in "$@"; do printf '%s\0' "$a" >>"$log"; done
printf '@@\0' >>"$log"
prev=; out=
for a in "$@"; do
	if [ "$prev" = "-o" ]; then out=$a; fi
	case "$a" in /out:*) out=${a#/out:} ;; esac
	prev=$a
done
[ -n "$out" ] && : >"$out"
exit 0
"""


def run_build(tag, build_id="__unset__"):
    """Run a private copy of build.sh against mock clang and lld-link. The production xnuloader/
    directory and its real loader.obj are never touched. A sentinel loader.obj stands in for a
    previous build so the reject paths can show no tool ran. Returns the process, the recorded
    argv per call, and the sentinel and loader.efi states afterwards."""
    d = os.path.join(BUILD, "buildsh-" + tag)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.join(d, "bin"))
    for tool in ("clang", "lld-link"):
        path = os.path.join(d, "bin", tool)
        with open(path, "w") as f:
            f.write(MOCK_TOOL)
        os.chmod(path, 0o755)
    with open(os.path.join(d, "loader.c"), "w") as f:
        f.write("/* the mock toolchain never reads the source */\n")
    sentinel = os.path.join(d, "loader.obj")
    with open(sentinel, "w") as f:
        f.write("OLD")   # a real object from an imaginary previous build
    shutil.copy(os.path.join(TOOLS, "..", "xnuloader", "build.sh"), os.path.join(d, "build.sh"))
    env = dict(os.environ, PATH=os.path.join(d, "bin") + os.pathsep + os.environ["PATH"],
               ARGVLOG=os.path.join(d, "argv.log"))
    env.pop("BUILD_ID", None)
    if build_id != "__unset__":
        env["BUILD_ID"] = build_id
    r = subprocess.run(["bash", os.path.join(d, "build.sh")], capture_output=True, text=True, env=env, cwd=d)
    calls = []
    if os.path.exists(env["ARGVLOG"]):
        with open(env["ARGVLOG"], "rb") as f:
            cur = []
            for p in f.read().split(b"\0"):
                if p == b"@@":
                    calls.append(cur)
                    cur = []
                elif p:
                    cur.append(p.decode())
    obj_now = open(sentinel).read() if os.path.exists(sentinel) else None
    return r, calls, obj_now, os.path.exists(os.path.join(d, "loader.efi"))


def compile_run_extra(c_text, name, extra_flags):
    cpath = os.path.join(BUILD, "buildid-%s.c" % name)
    xpath = os.path.join(BUILD, "buildid-%s" % name)
    with open(cpath, "w") as f:
        f.write(c_text)
    r = subprocess.run(["cc", "-std=gnu11", "-O0"] + extra_flags + ["-o", xpath, cpath], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("compile of %s failed:\n%s" % (name, r.stderr))
    r = subprocess.run([xpath], capture_output=True, text=True)
    return r.returncode, r.stdout


def main():
    src = _base.read_tu(LOADER)
    fails = []

    def ck(cond, name):
        if not cond:
            fails.append("FAIL wiring_%s" % name)

    blk_b = _base.extract(src, "LC-BID")
    blk_s = _base.extract(src, "LC-STR")
    ck("str_eq(const char *a, const char *b)" in blk_s, "strcmp_block")
    ck('str_eq(var, "build-id")' in blk_b and 'fb_reply("OKAY", LOADER_BUILD_ID);' in blk_b, "bid_branch_in_block")
    ck('#define LOADER_BUILD_ID "unknown"' in blk_b and blk_b.count("#ifndef LOADER_BUILD_ID") == 1, "bid_default_unknown")
    ck('fb_reply("OKAY", "xnuloader");' in blk_b and 'fb_reply("OKAY", "0.5");' in blk_b, "identity_vars_in_block")
    ck(src.count('fb_reply("OKAY", "xnuloader");') == 1 and src.count('fb_reply("OKAY", "0.5");') == 1, "identity_strings_once")
    ck(src.count("if (fb_getvar_identity(var)) {") == 1, "getvar_calls_identity_block")
    ck(src.count("static int\nfb_getvar_identity(") == 1, "identity_block_defined")
    sh = _base.read(os.path.join(TOOLS, "..", "xnuloader", "build.sh"))
    ck("^[0-9a-f]{40}$" in sh and "bid_flags=(" in sh and '"${bid_flags[@]}"' in sh, "buildsh_array_and_check")
    ck('bid_flags=("-DLOADER_BUILD_ID=\\"${BUILD_ID}\\"")' in sh, "buildsh_macro_keeps_quotes")
    ck("${BUILD_ID+x}" in sh and "BUILD_ID unset: getvar build-id will report unknown" in sh, "buildsh_unset_warning")
    ck("BUILD_ID is set but empty" in sh, "buildsh_empty_rejected_message")
    ck("eval" not in sh, "buildsh_no_eval")

    syntax = ["clang", "--target=aarch64-unknown-windows", "-ffreestanding", "-fno-builtin", "-fno-stack-protector",
              "-fshort-wchar", "-mgeneral-regs-only", "-O1", "-Wall", "-Wextra", "-Wno-unused-parameter", "-fsyntax-only"]
    r = subprocess.run(syntax + [LOADER], capture_output=True, text=True)
    ck(r.returncode == 0, "syntax_plain" + (("\n" + r.stderr) if r.returncode else ""))
    r = subprocess.run(syntax + ["-DLOADER_BUILD_ID=\"" + BID + "\"", LOADER], capture_output=True, text=True)
    ck(r.returncode == 0, "syntax_with_bid" + (("\n" + r.stderr) if r.returncode else ""))

    r, calls, obj, efi = run_build("unset")
    clangs = [c for c in calls if c and c[0] == "clang"]
    ck(r.returncode == 0 and "BUILD_ID unset" in r.stderr and len(clangs) == 1
       and not any(a.startswith("-DLOADER_BUILD_ID=") for a in clangs[0]) and obj == "" and efi,
       "buildsh_unset_warns_and_compiles_without_flag")

    r, calls, obj, efi = run_build("valid", BID)
    clangs = [c for c in calls if c and c[0] == "clang"]
    macro_args = [a for a in (clangs[0] if clangs else []) if a.startswith("-DLOADER_BUILD_ID=")]
    ck(r.returncode == 0 and len(clangs) == 1 and macro_args == ['-DLOADER_BUILD_ID="' + BID + '"']
       and obj == "" and efi, "buildsh_valid_passes_quoted_string_arg")

    # the captured argument goes to the real C compiler as one argv element, exactly as the
    # script handed it over: an unquoted hex token would define a number and fail here
    consumer = os.path.join(BUILD, "buildid-consumer.c")
    with open(consumer, "w") as f:
        f.write("static const char want[] = \"" + BID + "\";\n"
                "static const char *id = LOADER_BUILD_ID;\n"
                "int main(void)\n{\n\tint i;\n\tfor (i = 0; i < 40; i++) { if (id[i] != want[i]) { return 1; } }\n"
                "\treturn id[40] != 0;\n}\n")
    f = os.path.join(BUILD, "buildid-consumer")
    cc = subprocess.run(["cc", "-std=gnu11", "-O0"] + macro_args + ["-o", f, consumer], capture_output=True, text=True)
    ck(cc.returncode == 0 and (subprocess.run([f], capture_output=True, text=True).returncode == 0
       if cc.returncode == 0 else False), "buildsh_captured_macro_is_a_c_string"
       + (("\n" + cc.stderr) if cc.returncode else ""))
    bare = subprocess.run(["cc", "-std=gnu11", "-O0", "-DLOADER_BUILD_ID=" + BID, "-o", f + "-bare", consumer],
                          capture_output=True, text=True)
    ck(bare.returncode != 0, "unquoted_macro_rejected_by_cc")   # keeps the check above discriminating

    r, calls, obj, efi = run_build("empty", "")
    ck(r.returncode != 0 and calls == [] and obj == "OLD" and not efi and "set but empty" in r.stderr,
       "buildsh_empty_rejected_before_compiler")

    for tag, bad in (("x", "x"), ("short", BID[:-1]), ("upper", BID.upper()), ("nonhex", "z" + BID[1:]),
                     ("space", BID[:-1] + " "), ("newline", BID[:-1] + "\n")):
        r, calls, obj, efi = run_build("bad-" + tag, bad)
        ck(r.returncode != 0 and "40-hex" in r.stderr and calls == [] and obj == "OLD" and not efi,
           "buildsh_rejects_" + tag)

    rc, out = compile_run_extra(build_c(src, "unknown"), "bid-plain", [])
    ck(rc == 0 and "build-id checks passed" in out, "bid_plain_reports_unknown\n" + out)
    rc, out = compile_run_extra(build_c(src, BID), "bid-given", ["-DLOADER_BUILD_ID=\"" + BID + "\""])
    ck(rc == 0 and "build-id checks passed" in out, "bid_define_reports_value\n" + out)

    mutations = [
        ("mut_bid_value", '\t\tfb_reply("OKAY", LOADER_BUILD_ID);\n', '\t\tfb_reply("OKAY", "0.5");\n'),
        ("mut_bid_default", '#define LOADER_BUILD_ID "unknown"   /* no build-time id was given: the answer says so */',
                           '#define LOADER_BUILD_ID "0.5"   /* mutated */'),
        ("mut_bid_branch", '\t} else if (str_eq(var, "build-id")) {\n\t\tfb_reply("OKAY", LOADER_BUILD_ID);\n', '\t'),
    ]
    for name, old, new in mutations:
        if src.count(old) != 1:
            print("mutation %s: source text changed, update the test" % name)
            sys.exit(1)
        rc, out = _base.compile_run(build_c(src.replace(old, new, 1), "unknown"), name)
        if rc == 0 or "FAIL bid_" not in out:
            print("mutation %s NOT caught (rc=%d):\n%s" % (name, rc, out))
            sys.exit(1)
        print("mutation check: %-16s caught (%s)" % (name, [l for l in out.split("\n") if l.startswith("FAIL")][0]))

    if fails:
        print("\n".join(fails))
        sys.exit(1)
    print("build-id: wiring, syntax, build.sh input checks and both host builds passed")


if __name__ == "__main__":
    main()
