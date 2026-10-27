#!/bin/bash
# Save the xnu-12377 kernel work as it is in the working tree, in the forms xnu26/apply.sh puts
# back: xnu26/patches/ (the changes to Apple's files: one piece per changed file, named
# NN-<path>.patch with each / of the path spelled %, NN its line in patches/series) and
# xnu26/files/ (the new files that differ from, or are not in, xnu/files/). A file of xnu/files/
# that the tree has unchanged stays there only; one the tree does not have is reported (it
# belongs in xnu26/files.skip).
# usage: xnu26/export.sh [KERNEL-TREE]     (default: $XNU26_SRC, else xnu-src/xnu-12377.121.6)
set -e
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SRC=${1:-${XNU26_SRC:-$HERE/../xnu-src/xnu-12377.121.6}}
SHARED=$HERE/../xnu/files
BASE=ac9718fb1af618d5ce8678d0dc6e8a58f252216f           # tag xnu-12377.121.6
JUNK='^BUILD/|(^|/)__pycache__/|\.(orig|rej|pyc|o|swp)$|\.bak[0-9]*$|~$|(^|/)\.DS_Store$'

cd "$SRC"
git cat-file -e "$BASE^{commit}" 2>/dev/null || { echo "export.sh: $SRC is not a clone of xnu at $BASE" >&2; exit 1; }

rm -rf "$HERE/patches.new"
mkdir "$HERE/patches.new"
: > "$HERE/patches.new/series"
n=0
git diff --no-renames --name-only --diff-filter=MDT "$BASE" -- . | while IFS= read -r f; do
	n=$((n + 1))
	piece=$(printf '%02d-%s.patch' "$n" "${f//\//%}")
	git -c core.abbrev=12 diff --binary --no-renames --no-color --no-ext-diff "$BASE" -- "$f" > "$HERE/patches.new/$piece"
	echo "$piece" >> "$HERE/patches.new/series"
done
rm -rf "$HERE/patches"
mv "$HERE/patches.new" "$HERE/patches"

rm -rf "$HERE/files.new"
mkdir "$HERE/files.new"
{ git diff --no-renames --name-only --diff-filter=A "$BASE" -- .; git ls-files --others --exclude-standard; } |
	sort -u | grep -Ev "$JUNK" | while IFS= read -r f; do
	[ -f "$f" ] || [ -L "$f" ] || continue
	cmp -s "$f" "$SHARED/$f" && continue
	mkdir -p "$HERE/files.new/$(dirname "$f")"
	cp -P -p "$f" "$HERE/files.new/$f"
done
(cd "$SHARED" && find . -type f -o -type l) | sed 's|^\./||' | grep -vxF -f "$HERE/files.skip" | while IFS= read -r f; do
	[ -e "$f" ] || echo "export.sh: $f (xnu/files) is not in the tree: delete it there or name it in xnu26/files.skip" >&2
done
rm -rf "$HERE/files"
mv "$HERE/files.new" "$HERE/files"
echo "xnu26/patches: $(wc -l < "$HERE/patches/series" | tr -d ' ') pieces; xnu26/files: $(find "$HERE/files" -type f | wc -l | tr -d ' ') files"
