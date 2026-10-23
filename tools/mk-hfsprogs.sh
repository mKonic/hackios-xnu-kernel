#!/bin/bash
# mkfs.hfsplus for making /private/var's HFS+ volume under QEMU (qemu/run-ios.sh QEMU_VAR=hfs): the system's
# when there is one, else the AUR recipe of Apple's diskdev_cmds for Linux (hfsprogs), built here and not
# installed. Prints the program's path.
set -e
command -v mkfs.hfsplus && exit 0
ROOT=$(cd "$(dirname "$0")/.." && pwd)
D=$ROOT/xnu-deps/build/hfsprogs
M=$D/pkg/hfsprogs/usr/bin/mkfs.hfsplus
if [ ! -x "$M" ]; then
	rm -rf "$D"
	git clone -q https://aur.archlinux.org/hfsprogs.git "$D"
	(cd "$D" && makepkg --nodeps -f >/dev/null 2>&1)
fi
echo "$M"
