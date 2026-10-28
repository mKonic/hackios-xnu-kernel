#!/usr/bin/env python3
"""Host control-flow test for the XNU payload terminal path (BootManagerLib).

The real XnuLoader.c and XnuLoader.h are compiled and run: only the #include
lines are dropped and the block between the XNU-TERM markers is replaced by a
halt that records the event and longjmps, so a host process can observe what
the production control flow did. Everything between the markers is checked
before the swap (DAIF mask, self-loop, no calls), and the swap itself must
find the markers exactly once. Payload, Boot Services and Runtime Services are
host stubs that record every call in order; LocateHandleBuffer, HandleProtocol,
GetMemoryMap, LocateProtocol, SetWatchdogTimer, ResetSystem, SetVariable, GetTime,
AsciiVSPrint and the allocators count as forbidden. Nineteen scenarios cover a
missing, bad, oversized, empty, truncated, out-of-range and zero-size payload,
a non-16-byte-aligned entry before the loader, the declared body at the
container cap minus the header, one byte past it and the whole container size,
entry data ending exactly at End and one byte short of it, LoadImage failure,
StartImage returning success or an error, StartImage that never returns, and a
container built from independent "XNUP" wire bytes. Eight mutations prove the
checks bite: making the halt return must show XnuLoaderMain returning,
re-inserting one diagnostic write must show a forbidden call, dropping the
magic check, the size cap, the 16-byte entry padding or stepping by a fixed 24
bytes must each break the scenario suite, dropping the header subtraction from
the size cap must accept an over-by-one body, and retargeting the magic macro
in the header must reject the independent wire bytes. The AArch64 halt body
itself is verified by cross disassembly outside this test; the host run carries
no hardware semantics.

The Mu-Silicium root under test is optional argv[1] or $MU_SILICIUM_ROOT and
defaults to <kernel-root>/Mu-Silicium, so the test also runs from this tracked
location against a dirty Mu checkout; a missing root stops the test with a
clear message. XnuLoader.c and XnuLoader.h in that root must match the tracked
overlay copies in uefi/files byte for byte -- the assertions below were
established on those bytes, so the check tests the real port instead of a
look-alike and any edit only needs the two trees synced once. The size
expectations read XNU_PAYLOAD_MAX_SIZE out of XnuLoader.h; the "XNUP" wire
magic is an independent contract here: the header macro value is compared
against this file's own constant, one positive case builds the magic from raw
bytes, and a header-only retarget must fail. BootManagerLib.inf is verified by
content checks only.
Usage: python3 tools/test-xnu-payload-terminal.py [Mu-Silicium-root]"""

import os
import re
import struct
import subprocess
import sys
import tempfile

TOOLS = os.path.dirname(os.path.abspath(__file__))
KERNEL = os.path.dirname(TOOLS)
DEFAULT_MU = os.path.join(KERNEL, "Mu-Silicium")
OVERLAY = os.path.join(KERNEL, "uefi", "files", "Silicon", "Silicium",
                       "SiliciumPkg", "Library", "BootManagerLib")

# Wire contract for the XNUP header magic, kept independent of XnuLoader.h on
# purpose: every positive case could be built from the production macro, so a
# retargeted macro would keep passing a changed wire format silently.
WIRE_MAGIC = 0x50554E58
WIRE_MAGIC_BYTES = b"XNUP"


def resolve_mu_root():
    if len(sys.argv) == 2 and sys.argv[1] not in ("-h", "--help"):
        return os.path.abspath(sys.argv[1])
    if len(sys.argv) > 1:
        sys.exit("usage: test-xnu-payload-terminal.py [Mu-Silicium-root] "
                 "(default %s, env MU_SILICIUM_ROOT also honored)" % DEFAULT_MU)
    return os.path.abspath(os.environ.get("MU_SILICIUM_ROOT", DEFAULT_MU))


BEG, END = "// XNU-TERM-BEGIN", "// XNU-TERM-END"

PRELUDE = r"""
#include <stdarg.h>
#include <setjmp.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef uint8_t UINT8; typedef uint16_t UINT16; typedef uint32_t UINT32; typedef uint64_t UINT64;
typedef uint64_t UINTN;  typedef int64_t INTN;   typedef unsigned char BOOLEAN;
typedef char CHAR8;      typedef uint16_t CHAR16;
#define VOID void
#define STATIC static
#define EFIAPI
#define IN
#define OUT
#define TRUE 1
#define FALSE 0
#ifndef NULL
#define NULL ((void *)0)
#endif
#define ALIGN_VALUE(Value, Alignment) ((Value) + ((((Alignment) - ((Value) % (Alignment)))) % (Alignment)))
typedef void *EFI_HANDLE;
typedef UINTN EFI_STATUS;
#define EFI_SUCCESS     ((EFI_STATUS)0)
#define EFI_LOAD_ERROR  ((EFI_STATUS)0x8000000000000007ULL)
#define EFI_ERROR(a)    (((INTN)(a)) < 0)

static UINT8 g_payload[XNU_HOST_MAX + 4096u];  /* the reserved region, hosted; XNU_HOST_MAX injected from XnuLoader.h */
static jmp_buf g_jmp;

/* ---- call recorder: order matters, so every stub lands in one list ---- */
static const char *g_events[64];
static int g_event_count;
static int g_term_count;
static int g_forbidden_count;
static int g_returned;

static void Record(const char *who) { if (g_event_count < 64) { g_events[g_event_count++] = who; } }
static void Forbidden(void) { g_forbidden_count++; }

/* ---- Boot Services: production may only use LoadImage and StartImage ---- */
static UINTN g_li_size;
static UINTN g_li_offset;
static EFI_STATUS g_loadimage_rc;
static int g_startimage_mode;   /* 0 returns success, 1 returns error, 2 never returns */

static EFI_STATUS EFIAPI StubLoadImage(BOOLEAN BootPolicy, EFI_HANDLE Parent, void *DevicePath,
                                       void *Buffer, UINTN BufferSize, EFI_HANDLE *ImageHandle)
{
    (void)BootPolicy; (void)Parent; (void)DevicePath;
    Record("LoadImage");
    g_li_size = BufferSize;
    g_li_offset = (UINTN)((UINT8 *)Buffer - g_payload);
    if (g_loadimage_rc == EFI_SUCCESS) { *ImageHandle = (EFI_HANDLE)(UINTN)0xABCD0000; }
    return g_loadimage_rc;
}

static EFI_STATUS EFIAPI StubStartImage(EFI_HANDLE ImageHandle, UINTN *ExitDataSize, CHAR16 **ExitData)
{
    (void)ImageHandle; (void)ExitDataSize; (void)ExitData;
    Record("StartImage");
    if (g_startimage_mode == 2) { longjmp(g_jmp, 2); }
    return g_startimage_mode == 1 ? EFI_LOAD_ERROR : EFI_SUCCESS;
}

static EFI_STATUS EFIAPI StubLocateHandleBuffer(int a, void *b, void *c, UINTN *d, void **e)
{ (void)a; (void)b; (void)c; (void)d; (void)e; Record("LocateHandleBuffer"); Forbidden(); return EFI_LOAD_ERROR; }
static EFI_STATUS EFIAPI StubHandleProtocol(void *a, void *b, void **c)
{ (void)a; (void)b; (void)c; Record("HandleProtocol"); Forbidden(); return EFI_LOAD_ERROR; }
static EFI_STATUS EFIAPI StubGetMemoryMap(UINTN *a, void *b, UINTN *c, UINTN *d, UINT32 *e)
{ (void)a; (void)b; (void)c; (void)d; (void)e; Record("GetMemoryMap"); Forbidden(); return EFI_LOAD_ERROR; }
static EFI_STATUS EFIAPI StubLocateProtocol(void *a, void *b, void **c)
{ (void)a; (void)b; (void)c; Record("LocateProtocol"); Forbidden(); return EFI_LOAD_ERROR; }
static EFI_STATUS EFIAPI StubSetWatchdogTimer(UINT64 a, UINT64 b, UINT64 c, void *d)
{ (void)a; (void)b; (void)c; (void)d; Record("SetWatchdogTimer"); Forbidden(); return EFI_LOAD_ERROR; }

typedef struct {
    EFI_STATUS (EFIAPI *LoadImage)(BOOLEAN, EFI_HANDLE, void *, void *, UINTN, EFI_HANDLE *);
    EFI_STATUS (EFIAPI *StartImage)(EFI_HANDLE, UINTN *, CHAR16 **);
    EFI_STATUS (EFIAPI *LocateHandleBuffer)(int, void *, void *, UINTN *, void **);
    EFI_STATUS (EFIAPI *HandleProtocol)(void *, void *, void **);
    EFI_STATUS (EFIAPI *GetMemoryMap)(UINTN *, void *, UINTN *, UINTN *, UINT32 *);
    EFI_STATUS (EFIAPI *LocateProtocol)(void *, void *, void **);
    EFI_STATUS (EFIAPI *SetWatchdogTimer)(UINT64, UINT64, UINT64, void *);
} BS_STUB;
static BS_STUB g_bs_table = { StubLoadImage, StubStartImage, StubLocateHandleBuffer, StubHandleProtocol,
                              StubGetMemoryMap, StubLocateProtocol, StubSetWatchdogTimer };
static BS_STUB *const gBS = &g_bs_table;
static EFI_HANDLE gImageHandle = (EFI_HANDLE)(UINTN)0x1234;

/* ---- Runtime Services and old-diagnostic helpers: any use is forbidden ---- */
typedef struct {
    void (*ResetSystem)(int a, EFI_STATUS b, UINTN c, void *d);
    EFI_STATUS (*SetVariable)(void *, void *, UINT32, UINTN, void *);
    EFI_STATUS (*GetTime)(void *, void *);
} RT_STUB;
static void RTReset(int a, EFI_STATUS b, UINTN c, void *d)
{ (void)a; (void)b; (void)c; (void)d; Record("ResetSystem"); Forbidden(); }
static EFI_STATUS RTSetVariable(void *a, void *b, UINT32 c, UINTN d, void *e)
{ (void)a; (void)b; (void)c; (void)d; (void)e; Record("SetVariable"); Forbidden(); return EFI_LOAD_ERROR; }
static EFI_STATUS RTGetTime(void *a, void *b)
{ (void)a; (void)b; Record("GetTime"); Forbidden(); return EFI_LOAD_ERROR; }
static RT_STUB g_rt_table = { RTReset, RTSetVariable, RTGetTime };
static RT_STUB *const gRT = &g_rt_table;

static int AsciiVSPrint(void *Buf, UINTN Size, const CHAR8 *Fmt, ...)
{ (void)Buf; (void)Size; (void)Fmt; Record("AsciiVSPrint"); Forbidden(); return 0; }
static void *AllocateZeroPool(UINTN Size) { (void)Size; Record("AllocateZeroPool"); Forbidden(); return NULL; }
static void *AllocatePool(UINTN Size) { (void)Size; Record("AllocatePool"); Forbidden(); return NULL; }
static void FreePool(void *Buf) { (void)Buf; Record("FreePool"); Forbidden(); }
static int AsciiStrnCmp(const CHAR8 *a, const CHAR8 *b, UINTN n) { return strncmp(a, b, n); }
static void ProbeLogWrite(void) { Record("LogWrite"); Forbidden(); }   /* mutation M2 re-inserts this call */

/* ---- payload building ---- */
static void put_u32(UINT8 *p, UINT32 v) { memcpy(p, &v, 4); }
static UINTN add_entry(UINTN off, const char *name, UINT32 size)
{
    UINTN i; UINTN padded = (size + 15u) & ~(UINTN)15u;
    for (i = 0; i < 16; i++) { g_payload[off + i] = (i < strlen(name)) ? (UINT8)name[i] : 0; }
    put_u32(g_payload + off + 16, size);
    put_u32(g_payload + off + 20, 0);
    for (i = 0; i < size; i++) { g_payload[off + 24 + i] = (UINT8)(i * 7 + 1); }
    return off + 24 + padded;
}
"""

TERMINAL = r"""
__attribute__((noreturn))
STATIC
VOID
EFIAPI
XnuTerminalHalt (
  VOID
  )
{
  g_term_count++;
  Record("TERMINAL");
  longjmp(g_jmp, 1);
}
"""

# Parse-mutation counterexamples (review U3): each pair replaces production
# source text, and the named check must then fail. The first scenario set let
# all four through because the bad-magic container held no entries, everything
# after the oversized header was zero, and every entry length was a multiple
# of 16. Anchors are asserted unique when applied, so a production edit that
# moves one stops the test instead of silently skipping its mutation.
CHECK_LINE = ("if ((Header->Magic != XNU_PAYLOAD_MAGIC) || "
              "(Header->Size > XNU_PAYLOAD_MAX_SIZE - sizeof (XNU_PAYLOAD_HEADER))) {")
CAP_NO_HEADER = ("if ((Header->Magic != XNU_PAYLOAD_MAGIC) || "
                 "(Header->Size > XNU_PAYLOAD_MAX_SIZE)) {")

PARSE_MUTATIONS = {
    "magic_check": (CHECK_LINE,
                    "if ((Header->Size > XNU_PAYLOAD_MAX_SIZE - sizeof (XNU_PAYLOAD_HEADER))) {",
                    "FAIL missing_magic_events"),
    "size_cap": (CHECK_LINE, "if ((Header->Magic != XNU_PAYLOAD_MAGIC)) {",
                 "FAIL size_too_big_events"),
    "entry_padding": ("PaddedSize = ALIGN_VALUE ((UINTN)Entry->Size, 16);",
                      "PaddedSize = ((UINTN)Entry->Size);",
                      "FAIL unaligned_entry_skipped_events"),
    "fixed_step": ("Cursor += sizeof (XNU_PAYLOAD_ENTRY) + PaddedSize;",
                   "Cursor += sizeof (XNU_PAYLOAD_ENTRY);",
                   "FAIL unaligned_entry_skipped_events"),
}

# Length-contract mutation (review U1): the annibale cap bounds the declared
# body by container minus header; dropping the subtraction re-opens the old
# redwood off-by-eight window (body == container size read 8 bytes of tail).
BOUNDARY_MUTATIONS = {
    "cap_off_by_one": (CHECK_LINE, CAP_NO_HEADER, "FAIL body_over_by_one_events"),
}

# Wire-format mutation (review U4 gap): only the header macro is retargeted,
# both trees moving together; the scenario built from independent bytes must
# reject what the retargeted production code now accepts.
HEADER_MUTATIONS = {
    "wire_magic": ("0x50554E58", "0x50554E59", "FAIL wire_bytes_container_events"),
}

BODY_BOUNDARIES = r"""
    /* container-cap boundaries: the cap is XNU_PAYLOAD_MAX_SIZE minus the
       8-byte header, so body == cap - 8 loads, + 1 or the whole container
       size does not (the old redwood check accepted the latter two). */
    reset(); { end = add_entry(8, "loader.efi", 64); put_u32(g_payload, XNU_PAYLOAD_MAGIC); put_u32(g_payload + 4, (UINT32)(XNU_PAYLOAD_MAX_SIZE - sizeof (XNU_PAYLOAD_HEADER))); }
    call_main(); expect("body_max_accepted", "LoadImage,StartImage,TERMINAL", 1, 0);
    CK(g_li_size == 64 && g_li_offset == 32, "body_max_accepted", "loader_bytes_passed");
    reset(); { end = add_entry(8, "loader.efi", 64); put_u32(g_payload, XNU_PAYLOAD_MAGIC); put_u32(g_payload + 4, (UINT32)(XNU_PAYLOAD_MAX_SIZE - sizeof (XNU_PAYLOAD_HEADER)) + 1u); }
    call_main(); expect("body_over_by_one", "TERMINAL", 1, 0);
    reset(); { end = add_entry(8, "loader.efi", 64); put_u32(g_payload, XNU_PAYLOAD_MAGIC); put_u32(g_payload + 4, (UINT32)XNU_PAYLOAD_MAX_SIZE); }
    call_main(); expect("body_exact_container", "TERMINAL", 1, 0);
    reset(); { end = add_entry(8, "loader.efi", 16); put_u32(g_payload, XNU_PAYLOAD_MAGIC); put_u32(g_payload + 4, (UINT32)(end - 8)); }
    call_main(); expect("entry_end_exact", "LoadImage,StartImage,TERMINAL", 1, 0);
    reset(); { end = add_entry(8, "loader.efi", 16); put_u32(g_payload, XNU_PAYLOAD_MAGIC); put_u32(g_payload + 4, (UINT32)(end - 8) - 1u); }
    call_main(); expect("entry_end_short_one", "TERMINAL", 1, 0);
    /* wire-bytes positive: raw b"XNUP" little-endian, not the production macro */
    reset(); { end = add_entry(8, "loader.efi", 64); put_u32(g_payload, 0x50554E58u); put_u32(g_payload + 4, (UINT32)(end - 8)); }
    call_main(); expect("wire_bytes_container", "LoadImage,StartImage,TERMINAL", 1, 0);
    CK(g_li_size == 64 && g_li_offset == 32, "wire_bytes_container", "loader_bytes_passed");
"""

SCENARIOS = r"""
/* ---- scenario harness ---- */
static int g_fails;

static void CK(int cond, const char *name, const char *what)
{
    if (!cond) { printf("FAIL %s_%s\n", name, what); g_fails++; }
}

static void join(char *out, size_t n)
{
    size_t used = 0;
    int i;
    out[0] = 0;
    for (i = 0; i < g_event_count; i++) {
        int w = snprintf(out + used, n - used, "%s%s", (i ? "," : ""), g_events[i]);
        if (w < 0 || (size_t)w >= n - used) { break; }
        used += (size_t)w;
    }
}

static void reset(void)
{
    memset(g_payload, 0, sizeof(g_payload));
    memset(g_events, 0, sizeof(g_events));
    g_event_count = g_term_count = g_forbidden_count = g_returned = 0;
    g_loadimage_rc = EFI_SUCCESS;
    g_startimage_mode = 0;
    g_li_size = 0xFFFFFFFFu;
}

static void call_main(void)
{
    int code = setjmp(g_jmp);
    if (code == 0) {
        XnuLoaderMain();
        g_returned = 1;      /* code 0: the halt fell through, code 2: payload is running */
    }
}

static void expect(const char *name, const char *want_events, int want_term, int want_returned)
{
    char joined[512];
    join(joined, sizeof(joined));
    CK(strcmp(joined, want_events) == 0, name, "events");
    printf("%s events=%s term=%d forbidden=%d returned=%d\n", name, joined, g_term_count, g_forbidden_count, g_returned);
    CK(g_term_count == want_term, name, "term");
    CK(g_returned == want_returned, name, "returned");
    CK(g_forbidden_count == 0, name, "forbidden");
}

int main(void)
{
    UINTN end;

    reset(); { end = add_entry(8, "loader.efi", 64); put_u32(g_payload, 0x12345678u); put_u32(g_payload + 4, (UINT32)(end - 8)); }
    call_main(); expect("missing_magic", "TERMINAL", 1, 0);
    reset(); { end = add_entry(8, "loader.efi", 64); put_u32(g_payload, XNU_PAYLOAD_MAGIC); put_u32(g_payload + 4, (UINT32)XNU_PAYLOAD_MAX_SIZE + 1u); }
    call_main(); expect("size_too_big", "TERMINAL", 1, 0);
    reset(); put_u32(g_payload, XNU_PAYLOAD_MAGIC); put_u32(g_payload + 4, 0);          call_main();
    expect("empty_container", "TERMINAL", 1, 0);
    reset(); put_u32(g_payload, XNU_PAYLOAD_MAGIC); put_u32(g_payload + 4, 24);         call_main();
    expect("empty_first_entry", "TERMINAL", 1, 0);
    reset(); { end = add_entry(8, "kernel", 16); put_u32(g_payload, XNU_PAYLOAD_MAGIC); put_u32(g_payload + 4, (UINT32)(end - 8 + 24)); }
    call_main(); expect("no_loader", "TERMINAL", 1, 0);
    reset(); { put_u32(g_payload, XNU_PAYLOAD_MAGIC); put_u32(g_payload + 4, 20); memcpy(g_payload + 8, "loader.efi", 10); }
    call_main(); expect("truncated_entry_header", "TERMINAL", 1, 0);
    reset(); { add_entry(8, "loader.efi", 16); put_u32(g_payload + 8 + 16, 0x10000000u); put_u32(g_payload, XNU_PAYLOAD_MAGIC); put_u32(g_payload + 4, 40); }
    call_main(); expect("entry_data_out_of_range", "TERMINAL", 1, 0);
    reset(); { add_entry(8, "loader.efi", 0); put_u32(g_payload, XNU_PAYLOAD_MAGIC); put_u32(g_payload + 4, 24); }
    call_main(); expect("loader_size_zero", "TERMINAL", 1, 0);
    reset(); { add_entry(8, "loader.efi", 64); put_u32(g_payload, XNU_PAYLOAD_MAGIC); put_u32(g_payload + 4, 88); g_loadimage_rc = EFI_LOAD_ERROR; }
    call_main(); expect("loadimage_fails", "LoadImage,TERMINAL", 1, 0);
    reset(); { add_entry(8, "loader.efi", 64); put_u32(g_payload, XNU_PAYLOAD_MAGIC); put_u32(g_payload + 4, 88); }
    call_main(); expect("startimage_returns_success", "LoadImage,StartImage,TERMINAL", 1, 0);
    CK(g_li_size == 64 && g_li_offset == 32, "valid", "loader_bytes_passed");
    reset(); { end = add_entry(8, "pad.bin", 17); end = add_entry(end, "loader.efi", 64); put_u32(g_payload, XNU_PAYLOAD_MAGIC); put_u32(g_payload + 4, (UINT32)(end - 8)); }
    call_main(); expect("unaligned_entry_skipped", "LoadImage,StartImage,TERMINAL", 1, 0);
    CK(g_li_size == 64 && g_li_offset == 88, "unaligned_entry_skipped", "loader_bytes_passed");
    reset(); { add_entry(8, "loader.efi", 64); put_u32(g_payload, XNU_PAYLOAD_MAGIC); put_u32(g_payload + 4, 88); g_startimage_mode = 1; }
    call_main(); expect("startimage_returns_error", "LoadImage,StartImage,TERMINAL", 1, 0);
    reset(); { add_entry(8, "loader.efi", 64); put_u32(g_payload, XNU_PAYLOAD_MAGIC); put_u32(g_payload + 4, 88); g_startimage_mode = 2; }
    call_main(); expect("startimage_does_not_return", "LoadImage,StartImage", 0, 0);
""" + BODY_BOUNDARIES + r"""
    if (g_fails == 0) { printf("payload-terminal checks passed\n"); } else { printf("%d failures\n", g_fails); }
    return g_fails > 127 ? 127 : g_fails;
}
"""


def host_constants(header_source):
    """Read the size cap out of the production header so the host stand-in for
    the reserved region tracks XnuLoader.h instead of repeating its number."""
    m = re.search(r"#define\s+XNU_PAYLOAD_MAX_SIZE\s+(0[xX][0-9A-Fa-f]+)", header_source)
    if not m:
        sys.exit("XnuLoader.h no longer defines XNU_PAYLOAD_MAX_SIZE as a plain hex "
                 "#define; update this test's host model to read the new form")
    return m.group(1)


def assemble(c_source, header_source, terminal, main_returned_mutation=False, log_write_mutation=False,
             parse_mutation=None, header_mutation=None):
    body = "\n".join(l for l in c_source.splitlines() if not l.startswith("#include"))
    beg, end = body.index(BEG), body.index(END) + len(END)
    for probe in ("daifset", "for (;;)"):
        assert probe in body[beg:end], "terminal block lost %s" % probe
    for absent in ("gBS->", "ResetSystem", "SetVariable", "CpuDeadLoop"):
        assert absent not in body[beg:end], "terminal block calls %s" % absent
    if parse_mutation:
        old, new, _ = {**PARSE_MUTATIONS, **BOUNDARY_MUTATIONS}[parse_mutation]
        assert body.count(old) == 1, "mutation %s: %d copies of its anchor in production source" % (
            parse_mutation, body.count(old))
        body = body.replace(old, new)
    if header_mutation:
        old, new, _ = HEADER_MUTATIONS[header_mutation]
        assert header_source.count(old) == 1, "mutation %s: %d copies of its anchor in the header" % (
            header_mutation, header_source.count(old))
        header_source = header_source.replace(old, new)
    body = body[:beg] + terminal + body[end:]
    if log_write_mutation:
        assert body.count("  XnuTerminalHalt ();") == 1
        body = body.replace("  XnuTerminalHalt ();", "  ProbeLogWrite ();\n  XnuTerminalHalt ();")
    if main_returned_mutation:
        body = body.replace(TERMINAL, "STATIC\nVOID\nEFIAPI\nXnuTerminalHalt (\n  VOID\n  )\n{\n  g_term_count++;\n  Record(\"TERMINAL\");\n}\n")
        header_source = header_source.replace("__attribute__((noreturn))\nVOID\nXnuLoaderMain ();", "VOID\nXnuLoaderMain ();")
    return ("#define XNU_HOST_MAX ((UINTN)%sULL)\n" % host_constants(header_source)
            + PRELUDE + header_source + "\n#undef XNU_PAYLOAD_BASE\n#define XNU_PAYLOAD_BASE ((UINTN)g_payload)\n" + body + SCENARIOS)


def compile_run(c_text, name, workdir):
    cpath = os.path.join(workdir, "terminal-%s.c" % name)
    xpath = os.path.join(workdir, "terminal-%s" % name)
    with open(cpath, "w") as f:
        f.write(c_text)
    r = subprocess.run(["cc", "-std=gnu11", "-O1", "-o", xpath, cpath], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("compile of %s failed:\n%s" % (name, r.stderr))
    return subprocess.run([xpath], capture_output=True, text=True)


def main():
    fails = []

    def ck(cond, name):
        if not cond:
            fails.append("FAIL wiring_%s" % name)

    src = os.path.join(resolve_mu_root(), "Silicon", "Silicium", "SiliciumPkg", "Library", "BootManagerLib")

    def tree_bytes(name):
        path = os.path.join(src, name)
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError as exc:
            sys.exit("cannot read production file %s (pass the Mu-Silicium root as argv[1] "
                     "or set MU_SILICIUM_ROOT): %s" % (path, exc))

    # The assertions below were established on the bytes the kernel repo exports
    # in uefi/files; require the root under test to match them byte for byte.
    # An edit then needs the two trees synced once, not a hash constant here
    # updated on every touched byte. BootManagerLib.inf is content-checked
    # further down instead of being byte-pinned.
    for name in ("XnuLoader.c", "XnuLoader.h"):
        overlay = os.path.join(OVERLAY, name)
        try:
            with open(overlay, "rb") as f:
                tracked = f.read()
        except OSError as exc:
            sys.exit("cannot read tracked overlay file %s: %s" % (overlay, exc))
        if tree_bytes(name) != tracked:
            sys.exit("%s differs byte for byte from the overlay copy %s\n"
                     "  sync one to the other, or point this test at the tree these "
                     "assertions were established on" % (os.path.join(src, name), overlay))

    c_source = tree_bytes("XnuLoader.c").decode()
    header_source = tree_bytes("XnuLoader.h").decode()
    inf = tree_bytes("BootManagerLib.inf").decode()

    # wiring: the diagnostics tail is gone and the halt is a plain masked loop
    ck(c_source.count(BEG) == 1 and c_source.count(END) == 1, "terminal_markers_once")
    for gone in ("FlushLog", "FindPartition", "ResetSystem", "SetVariable", "BlockIo", "PartitionInfo",
                 "AllocateZeroPool", "AsciiVSPrint", "GetTime", "gRT", "LOG_SIZE", "mLog", "wfi", "wfe"):
        ck(gone not in c_source, "%s_removed" % gone)
    ck(c_source.count("XnuTerminalHalt") == 2, "one_halt_definition_and_call")
    ck(c_source.count("StartPayloadLoader") == 2, "one_loader_definition_and_call")
    ck("XNU_LOG_PARTITION" not in header_source and "XNU_MARKER_MAGIC" not in header_source, "header_diagnostics_gone")
    ck("__attribute__((noreturn))\nVOID\nXnuLoaderMain ();" in header_source, "header_declares_noreturn")
    ck("gEfiBlockIoProtocolGuid" not in inf and "gEfiPartitionInfoProtocolGuid" not in inf, "inf_protocols_gone")
    for keep in ("UefiRuntimeServicesTableLib", "PrintLib", "MemoryAllocationLib"):   # other library users
        ck(keep in inf, "inf_keeps_%s" % keep.lower())

    # wire contract (review U4): the header macro must equal this file's own
    # XNUP constant, which is pinned to raw bytes and never derived from it
    ck(struct.pack("<I", WIRE_MAGIC) == WIRE_MAGIC_BYTES, "wire_constant_is_XNUP")
    mm = re.search(r"#define\s+XNU_PAYLOAD_MAGIC\s+(0[xX][0-9A-Fa-f]+)", header_source)
    ck(mm is not None, "header_defines_wire_magic")
    if mm:
        ck(int(mm.group(1), 16) == WIRE_MAGIC, "header_wire_magic_matches_independent_contract")

    with tempfile.TemporaryDirectory(prefix="xnu-terminal-") as workdir:
        r = compile_run(assemble(c_source, header_source, TERMINAL), "plain", workdir)
        ck(r.returncode == 0 and "payload-terminal checks passed" in r.stdout, "scenarios_pass\n" + r.stdout + r.stderr)
        for line in r.stdout.splitlines():
            if " forbidden=" in line:
                ck(" forbidden=0 " in line, "no_forbidden_calls")

        m1 = compile_run(assemble(c_source, header_source, TERMINAL, main_returned_mutation=True), "mut-return", workdir)
        ck(m1.returncode != 0 and "FAIL missing_magic_returned" in m1.stdout, "mutation_removed_halt_caught")

        m2 = compile_run(assemble(c_source, header_source, TERMINAL, log_write_mutation=True), "mut-write", workdir)
        ck(m2.returncode != 0 and "FAIL missing_magic_forbidden" in m2.stdout, "mutation_diagnostic_write_caught")

        for name in sorted(PARSE_MUTATIONS):
            want = PARSE_MUTATIONS[name][2]
            m = compile_run(assemble(c_source, header_source, TERMINAL, parse_mutation=name), "mut-" + name, workdir)
            ck(m.returncode != 0 and want in m.stdout, "parse_mutation_%s_caught" % name)

        for name in sorted(BOUNDARY_MUTATIONS):
            want = BOUNDARY_MUTATIONS[name][2]
            m = compile_run(assemble(c_source, header_source, TERMINAL, parse_mutation=name), "mut-" + name, workdir)
            ck(m.returncode != 0 and want in m.stdout, "boundary_mutation_%s_caught" % name)

        for name in sorted(HEADER_MUTATIONS):
            want = HEADER_MUTATIONS[name][2]
            m = compile_run(assemble(c_source, header_source, TERMINAL, header_mutation=name), "mut-" + name, workdir)
            ck(m.returncode != 0 and want in m.stdout, "contract_mutation_%s_caught" % name)

    # the include-strip must have kept the real bodies, not a look-alike model
    produced = assemble(c_source, header_source, TERMINAL)
    for real in ("PaddedSize = ALIGN_VALUE ((UINTN)Entry->Size, 16);", "Header->Magic != XNU_PAYLOAD_MAGIC",
                 "Header->Size > XNU_PAYLOAD_MAX_SIZE - sizeof (XNU_PAYLOAD_HEADER)",
                 "gBS->LoadImage (FALSE, gImageHandle, NULL, Entry + 1, Entry->Size, &Handle)"):
        ck(real in produced, "real_code_compiled")
    ck(len(re.findall(r"XnuLoaderMain", produced)) >= 3, "entry_symbol_present")

    if fails:
        print("\n".join(fails))
        sys.exit(1)
    print("payload-terminal: wiring, nineteen scenarios and eight mutations passed")


if __name__ == "__main__":
    main()
