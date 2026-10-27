#!/usr/bin/env python3
"""
Build the "XNUP" payload consumed by the boot shim and the XNU loader.

  header : u32 magic "XNUP", u32 size (bytes after the header)
  entries: char name[16], u32 size, u32 reserved, data padded to 16 bytes
  end    : an entry whose name is empty

usage: mkpayload.py -o payload.bin name=file [name=file ...] [--cmdline "..."]
       [--require name=sha256 ...] [--require-cmd TOKEN ...] [--forbid-cmd TOKEN ...]
       [--forbid-entry NAME ...]

Input checks run before assembly: every input file is read exactly once and
the same bytes feed the --require hash and the payload entry, so a file
replaced between the two reads cannot ship unapproved content. Item names are
validated (1..15 ASCII bytes, no NUL, unique, not a flag-owned name) because
struct.pack truncates char name[16] silently and the loader binds names with
strcmp semantics: a stored NUL aliases the prefix and the last duplicate
entry wins. --require-cmd demands an exact boot-arg token, --forbid-cmd
rejects an exact boot-arg key (bare or with =value), --exact-cmd demands the approved token set with
only the epoch value differing; while --exact-cmd is in use epoch values must
be non-negative decimal, and no boot-arg string may contain NUL.
--forbid-entry NAME (repeatable) refuses any payload entry, item- or
flag-derived; an approved experiment command uses it to forbid rdfile/rdres or
any other out-of-scope entry explicitly. Validation failures exit before
writing the output; if the output write itself fails, an existing file may be
left damaged. The payload's sha256 is printed so the run record can bind it.

Compatibility: builds that relied on duplicate item names (last-wins), item
names of 16+ bytes (silently truncated), NUL-bearing names, items colliding
with the cmdline/physbase/rdres/rdfile/usb/checkpoint flags, non-decimal epoch
values, or NUL bytes in boot-arg strings are rejected now; ordinary
kernel=/ramdisk= items plus flags build unchanged.
"""
import argparse, hashlib, struct, sys

FLAG_OWNED = ('cmdline', 'physbase', 'rdres', 'rdfile', 'usb', 'checkpoint', 'memsize', 'identity')

ap = argparse.ArgumentParser()
ap.add_argument('-o', '--output', required=True)
ap.add_argument('--cmdline', default='')
ap.add_argument('--physbase', default='', help='hex: force XNU physical base (testing)')
ap.add_argument('--memsize', default='', help="hex: the largest memory window, for a kernel whose bootstrap page tables cover it (default 6 GB)")
ap.add_argument('--identity', default='', choices=('', 'd22', 'v57'),
                help='the iPhone the device tree names: d22 (iPhone X, the default, iOS 16.5) or v57 (iPhone 17, iOS 26)')
ap.add_argument('--usb', action='store_true', help='make the loader stay resident and take commands over USB')
ap.add_argument('--rdres', default='', help='hex size of a root image already placed at the resident address (QEMU)')
ap.add_argument('--rdfile', default='', help='partition:size[:offset|tail] - staged system image on a GPT partition (phone)')
ap.add_argument('--checkpoint', default='', help='kernel checkpoint to stop at; 999 = loader self test')
ap.add_argument('--require', action='append', default=[], metavar='NAME=SHA256',
                help='refuse to build unless this input file hashes to this sha256')
ap.add_argument('--require-cmd', action='append', default=[], metavar='TOKEN',
                help='refuse to build unless the cmdline carries this exact token')
ap.add_argument('--forbid-cmd', action='append', default=[], metavar='TOKEN',
                help='refuse to build if the cmdline carries this exact boot-arg key, bare or with =value; a key merely starting with TOKEN does not match')
ap.add_argument('--exact-cmd', default='', metavar='CMDLINE',
                help='refuse to build unless the cmdline is the approved string; only the epoch value may differ')
ap.add_argument('--forbid-entry', action='append', default=[], metavar='NAME',
                help='refuse to build if the payload would carry an entry with this name (item or flag)')
ap.add_argument('items', nargs='*')
args = ap.parse_args()

inputs = {}
for item in args.items:
    name, sep, path = item.partition('=')
    if not sep or not path:
        sys.exit(f'item: expected name=path, got {item!r}')
    if not 1 <= len(name) <= 15 or '\0' in name or not name.isascii():
        sys.exit(f'item: name {name!r} must be 1..15 ASCII bytes without NUL '
                 f'(struct.pack truncates char name[16] at 16 bytes silently; the loader '
                 f'matches names with strcmp and stops at a NUL)')
    if name in FLAG_OWNED:
        sys.exit(f'item: {name} comes from the --{name} flag; refuse an unvalidated second entry point')
    if name in inputs:
        sys.exit(f'item: duplicate name {name} ({inputs[name]} and {path}); the loader would keep only the last')
    inputs[name] = path

# One read per input file; the bytes below are what both --require and the
# payload see.
blobs = {}
for name, path in inputs.items():
    try:
        with open(path, 'rb') as handle:
            blobs[name] = handle.read()
    except OSError as exc:
        sys.exit(f'item: {name} ({path}): {exc}')

for req in args.require:
    name, want = req.split('=', 1)
    if name not in inputs:
        sys.exit(f'require: no input named {name}')
    digest = hashlib.sha256(blobs[name]).hexdigest()
    if digest != want:
        sys.exit(f'require: {name} ({inputs[name]}) is {digest}, wanted {want}')

for where, text in (('cmdline', args.cmdline), ('exact-cmd', args.exact_cmd)):
    if '\0' in text:
        sys.exit(f'{where}: contains a NUL byte; the loader and kernel stop parsing at it')
tokens = args.cmdline.split()
for want in args.require_cmd:
    if want not in tokens:
        sys.exit(f'require-cmd: cmdline lacks {want}')
for bad in args.forbid_cmd:
    hit = [t for t in tokens if t == bad or t.startswith(bad + '=')]
    if hit:
        sys.exit(f'forbid-cmd: cmdline carries {hit[0]}')
if args.exact_cmd:
    def norm(ts):
        keep, epoch = [], None
        for t in ts:
            if t.startswith('epoch='):
                if epoch is not None:
                    sys.exit(f'exact-cmd: duplicate token {t}')
                value = t[len('epoch='):]
                if not (value.isascii() and value.isdigit()):
                    sys.exit(f'exact-cmd: epoch value must be non-negative decimal: {t!r}')
                epoch = t
            else:
                keep.append(t)
        return keep, epoch
    want, wepoch = norm(args.exact_cmd.split())
    got, gepoch = norm(tokens)
    if wepoch is None or gepoch is None:
        sys.exit('exact-cmd: both the approved string and the cmdline need one epoch token')
    if sorted(got) != sorted(want):
        extra = sorted(set(got) - set(want))
        missing = sorted(set(want) - set(got))
        dup = sorted({t for t in got if got.count(t) > want.count(t)})
        sys.exit(f'exact-cmd: extra={extra} missing={missing} duplicate={dup}')

# Entry names as they will serialize; --forbid-entry checks items and flags
# uniformly, so approval-scope rules never depend on which option supplied one.
entries = list(inputs)
for name, present in (('cmdline', args.cmdline), ('physbase', args.physbase),
                      ('rdres', args.rdres), ('rdfile', args.rdfile),
                      ('usb', args.usb), ('checkpoint', args.checkpoint), ('memsize', args.memsize),
                      ('identity', args.identity)):
    if present:
        entries.append(name)
for bad in args.forbid_entry:
    if not 1 <= len(bad) <= 15 or '\0' in bad or not bad.isascii():
        sys.exit(f"forbid-entry: name {bad!r} must be 1..15 ASCII bytes without NUL")
    if bad in entries:
        sys.exit(f'forbid-entry: the payload would carry {bad}')

body = b''
def add(name, data):
    global body
    body += struct.pack('<16sII', name.encode(), len(data), 0) + data + b'\0' * (-len(data) % 16)

for name in inputs:
    add(name, blobs[name])
if args.cmdline:
    add('cmdline', args.cmdline.encode() + b'\0')
if args.physbase:
    add('physbase', args.physbase.lower().replace('0x', '').encode() + b'\0')
if args.memsize:
    add('memsize', args.memsize.lower().replace('0x', '').encode() + b'\0')
if args.identity:
    add('identity', args.identity.encode() + b'\0')
if args.rdres:
    add('rdres', args.rdres.lower().replace('0x', '').encode() + b'\0')
if args.rdfile:
    add('rdfile', args.rdfile.encode() + b'\0')
if args.usb:
    add('usb', b'1\0')
if args.checkpoint:
    add('checkpoint', args.checkpoint.encode() + b'\0')
body += b'\0' * 24
payload = struct.pack('<II', 0x50554E58, len(body)) + body
open(args.output, 'wb').write(payload)
print(f'{args.output}: {len(payload)} bytes, sha256 {hashlib.sha256(payload).hexdigest()}')
