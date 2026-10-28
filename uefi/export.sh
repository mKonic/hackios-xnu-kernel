#!/usr/bin/env bash
# Save our changes to the Mu-Silicium tree (../Mu-Silicium, a clone of Project-Silicium/Mu-Silicium
# with the redwood port in it) into this directory: mu-silicium.patch for the files that upstream
# has, files/ for the ones it does not. apply.sh puts them into a fresh clone.
set -e
cd "$(dirname "$(readlink -f "$0")")"
MU=../Mu-Silicium

# Preflight: this script writes BASE and mu-silicium.patch and then replaces
# files/, and the only tree shape whose export is complete is what apply.sh
# produces -- HEAD at BASE, upstream-file edits unstaged in the work tree,
# and the new port files present. Refuse before any replacement on anything
# else:
#  - HEAD ahead of BASE (local commits, e.g. the current annibale history):
#    BASE would record a commit upstream does not have and the patch would
#    lose everything already committed;
#  - staged changes under BootShim/ or Silicon/: mu-silicium.patch comes
#    from git diff, which records work tree versus index only, so a staged
#    edit would silently vanish from the patch; unstage it and decide on
#    purpose whether the export should carry it;
#  - a port path missing or with the wrong kind (directory vs regular
#    file): the copy step would fail or export the wrong shape.
# BASE, mu-silicium.patch and files/ are then staged in a temporary
# directory and replaced only after the whole export exists and its required
# paths verified. That bounds the window of partially written output to the
# replacement steps themselves; the three replacements are separate moves
# over several files, so this makes no multi-file atomicity claim.
# tools/test-uefi-export.py checks the refusals, the untouched overlay on
# refusal and the apply.sh-shaped success against throwaway git fixtures.
die() { echo "uefi/export.sh: $*" >&2; exit 1; }
ref=$( [ -d "$MU" ] && git -C "$MU" rev-parse HEAD 2>/dev/null || true )
[ -n "$ref" ] || die "$MU is not a git checkout; nothing to export"
base=$(cat BASE 2>/dev/null) || die "BASE is unreadable; refusing to rewrite it"
[ -n "$base" ] || die "BASE is empty; refusing to rewrite it"
[ "$ref" = "$base" ] || die "$MU is at $ref but BASE is $base: committed port changes would drop out of the patch and BASE would record a possibly upstream-absent commit; export from a separate apply.sh-shaped checkout"
git -C "$MU" diff --cached --quiet -- BootShim Silicon || die "$MU has staged changes under BootShim/ or Silicon/ that git diff would leave out of mu-silicium.patch; unstage them first"
for want in "d:Platforms/Xiaomi/redwoodPkg" "f:Resources/Configs/redwood.toml" "f:Resources/DTBs/redwood.dtb" \
	"f:Silicon/Silicium/SiliciumPkg/Library/BootManagerLib/XnuLoader.c" \
	"f:Silicon/Silicium/SiliciumPkg/Library/BootManagerLib/XnuLoader.h" \
	"d:Binaries/redwood"; do
  kind=${want%%:*}; path=${want#*:}
  case $kind in
    d) [ -d "$MU/$path" ] || die "$MU/$path is not a directory: $MU does not carry the redwood port, files/ must not be replaced from it";;
    f) { [ -f "$MU/$path" ] && [ -r "$MU/$path" ]; } || die "$MU/$path is not a readable regular file: $MU does not carry the redwood port, files/ must not be replaced from it";;
  esac
done

tmp=$(mktemp -d "${TMPDIR:-/tmp}/uefi-export.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
git -C $MU rev-parse HEAD > "$tmp/BASE"
git -C $MU diff -- BootShim Silicon > "$tmp/mu-silicium.patch"
mkdir -p "$tmp/files/Resources/Configs" "$tmp/files/Resources/DTBs" "$tmp/files/Platforms/Xiaomi" "$tmp/files/Binaries" \
	"$tmp/files/Silicon/Silicium/SiliciumPkg/Library/BootManagerLib"
cp -r $MU/Platforms/Xiaomi/redwoodPkg "$tmp/files/Platforms/Xiaomi/"
cp $MU/Resources/Configs/redwood.toml "$tmp/files/Resources/Configs/"
cp $MU/Resources/DTBs/redwood.dtb "$tmp/files/Resources/DTBs/"
cp $MU/Silicon/Silicium/SiliciumPkg/Library/BootManagerLib/XnuLoader.[ch] "$tmp/files/Silicon/Silicium/SiliciumPkg/Library/BootManagerLib/"
cp -r $MU/Binaries/redwood "$tmp/files/Binaries/"
find "$tmp/files" -name __pycache__ -prune -exec rm -rf {} +
[ "$(cat "$tmp/BASE")" = "$ref" ] || die "staged BASE does not match $ref"
for want in "d:Platforms/Xiaomi/redwoodPkg" "f:Resources/Configs/redwood.toml" "f:Resources/DTBs/redwood.dtb" \
	"f:Silicon/Silicium/SiliciumPkg/Library/BootManagerLib/XnuLoader.c" \
	"f:Silicon/Silicium/SiliciumPkg/Library/BootManagerLib/XnuLoader.h" \
	"d:Binaries/redwood"; do
  kind=${want%%:*}; path=${want#*:}
  case $kind in
    d) [ -d "$tmp/files/$path" ] || die "staged export lost directory $path";;
    f) [ -f "$tmp/files/$path" ] || die "staged export lost file $path";;
  esac
done
mv "$tmp/BASE" BASE
mv "$tmp/mu-silicium.patch" mu-silicium.patch
rm -rf files
mv "$tmp/files" files
trap - EXIT
rm -rf "$tmp"
echo "uefi/export.sh: base $(cut -c1-12 BASE), patch $(wc -l < mu-silicium.patch) lines, $(find files -type f | wc -l) files"
