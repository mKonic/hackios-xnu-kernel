#!/usr/bin/env bash
# Save the kernel work as it is in the working tree, in the two forms apply.sh puts back:
#   patches/        the changes to Apple's files (against the tag the work sits on): one piece
#                   per changed file, named NN-<path>.patch with each / of the path spelled %,
#                   NN the line of that file in patches/series (a file per line, in the order
#                   apply.sh applies them -- git's own order, which is the paths sorted)
#   files/          the files that are not in Apple's tree, at their paths
# usage: xnu/export.sh [KERNEL-TREE]     (default: $XNU_SRC, else xnu-src/xnu-8796.121.2)
# Run it before committing whenever the kernel tree has changed. Committed or not makes no
# difference there: everything is compared with the base commit.
#
# One piece per file, so that two people changing two files no longer collide in one patch --
# the single redwood.patch that used to hold all of Apple's changes is retired by this export.
#
# The build container mounts the repository /src read-only, so running this there writes
# nothing: copy the script to a writable place (/work, /tmp), run it on the kernel tree, and
# take the patches/ and files/ it prints about back into the repository (docker cp, from the host).
set -e
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SRC=${1:-${XNU_SRC:-$HERE/../xnu-src/xnu-8796.121.2}}
BASE=c54f35ca767986246321eb901baf8f5ff7923f6a           # tag xnu-8796.121.2
# never part of the work: build output and what editors, patch and python leave behind
JUNK='^BUILD/|(^|/)__pycache__/|\.(orig|rej|pyc|o|swp)$|\.bak[0-9]*$|~$|(^|/)\.DS_Store$'
[ -w "$HERE" ] || { echo "export.sh: $HERE is read-only; run a copy of this script from a writable directory" >&2; exit 1; }

cd "$SRC"
git cat-file -e "$BASE^{commit}" 2>/dev/null || { echo "export.sh: $SRC is not a clone of xnu at $BASE" >&2; exit 1; }

# no renames: a moved file is a deletion in the series and a new file in files/
rm -rf "$HERE/patches.new"
mkdir "$HERE/patches.new"
git diff --no-renames --name-only --diff-filter=MDT "$BASE" -- . > "$HERE/patches.new/pieces.list"
: > "$HERE/patches.new/series"
n=0
while IFS= read -r f; do
	n=$((n + 1))
	piece=$(printf '%02d-%s.patch' "$n" "${f//\//%}")
	git -c core.abbrev=12 diff --binary --no-renames --no-color --no-ext-diff "$BASE" -- "$f" > "$HERE/patches.new/$piece"
	[ -s "$HERE/patches.new/$piece" ] || { echo "export.sh: $f came out with no changes; skipped" >&2; rm -f "$HERE/patches.new/$piece"; n=$((n - 1)); continue; }
	echo "$piece" >> "$HERE/patches.new/series"
done < "$HERE/patches.new/pieces.list"
rm -f "$HERE/patches.new/pieces.list"
rm -rf "$HERE/patches" "$HERE/redwood.patch"            # redwood.patch: retired, the series replaces it
mv "$HERE/patches.new" "$HERE/patches"

rm -rf "$HERE/files.new"
mkdir "$HERE/files.new"
{ git diff --no-renames --name-only --diff-filter=A "$BASE" -- .; git ls-files --others --exclude-standard; } |
	sort -u | grep -Ev "$JUNK" > "$HERE/files.new/files.list"
while IFS= read -r f; do
	[ -f "$f" ] || [ -L "$f" ] || continue             # added in a commit, deleted since
	mkdir -p "$HERE/files.new/$(dirname "$f")"
	cp -P --preserve=mode "$f" "$HERE/files.new/$f"
done < "$HERE/files.new/files.list"
rm -f "$HERE/files.new/files.list"
rm -rf "$HERE/files"
mv "$HERE/files.new" "$HERE/files"

echo "xnu/patches: $(wc -l < "$HERE/patches/series") pieces, $(du -sk "$HERE/patches" | cut -f1) KB"
echo "xnu/files: $(find "$HERE/files" -type f | wc -l) new files, $(du -sk "$HERE/files" | cut -f1) KB"
