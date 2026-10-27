#!/bin/bash
# Get Apple's xnu-12377.121.6 (macOS 26.5; the kernel interfaces iOS 26 uses, see
# docs/analysis/2026-10-26-ios26-27-kernel-abi.md in the top-level repository) and put the redwood
# work on top of it: xnu26/patches/ changes Apple's files, one piece per file, in the order
# xnu26/patches/series names them; the new files are those of xnu/files/ (shared with the
# xnu-8796 kernel) except the ones xnu26/files.skip names, then xnu26/files/ (what 12377 needs
# in a version of its own).
# usage: xnu26/apply.sh [DIR]        (default: $XNU26_SRC, else xnu-src/xnu-12377.121.6)
# env:   XNU_URL   where to fetch from (default: Apple's repository on GitHub)
# The result is a git tree on the branch "redwood26" with the work uncommitted, as
# xnu26/export.sh expects it. A directory that already exists is left alone.
set -e
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DIR=${1:-${XNU26_SRC:-$HERE/../xnu-src/xnu-12377.121.6}}
URL=${XNU_URL:-https://github.com/apple-oss-distributions/xnu.git}
TAG=xnu-12377.121.6
BASE=ac9718fb1af618d5ce8678d0dc6e8a58f252216f

[ -e "$DIR" ] && { echo "apply.sh: $DIR already exists; not touching it" >&2; exit 1; }
mkdir -p "$(dirname "$DIR")"
DIR=$(cd "$(dirname "$DIR")" && pwd)/$(basename "$DIR")
git init -q "$DIR"
trap 'echo "apply.sh: failed; removing the incomplete $DIR" >&2; rm -rf "$DIR"' ERR
cd "$DIR"
git remote add origin "$URL"
git fetch -q --depth 1 origin "refs/tags/$TAG:refs/tags/$TAG"
git checkout -q -b redwood26 "$TAG"
[ "$(git rev-parse HEAD)" = "$BASE" ] || { echo "apply.sh: $TAG is $(git rev-parse HEAD), expected $BASE" >&2; false; }
echo 'BUILD/' >> .git/info/exclude
while IFS= read -r piece; do
	git apply --whitespace=nowarn "$HERE/patches/$piece" || { echo "apply.sh: $piece did not apply" >&2; exit 1; }
done < "$HERE/patches/series"
(cd "$HERE/../xnu/files" && find . -type f -o -type l) | sed 's|^\./||' | grep -vxF -f "$HERE/files.skip" |
	while IFS= read -r f; do
		mkdir -p "$(dirname "$f")"
		cp -P -p "$HERE/../xnu/files/$f" "$f"
	done
[ -d "$HERE/files" ] && cp -R -P -p "$HERE/files/." .
echo "apply.sh: $DIR: $TAG + $(wc -l < "$HERE/patches/series" | tr -d ' ') pieces"
