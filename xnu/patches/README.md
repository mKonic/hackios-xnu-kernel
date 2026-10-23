# Numbered kernel patches

`xnu/apply.sh` applies these in order after `redwood.patch` and `files/`. They keep work on
separate tracks from conflicting in the one big patch:

| Numbers | Track |
|---|---|
| 1000-1099 | platform: AVF/crosvm (`avf/`) |
| 1100-1199 | virtio-pci transport and drivers |
| 1200-1299 | display and input |
| 1300-1399 | GPU 3D: virtio-gpu 3D / gfxstream (`bsd/dev/redwood_vgpu3d*.c`) |

Make one with `git diff` in the kernel tree against the state after `apply.sh`, and mind that
`xnu/export.sh` folds everything in the tree into `redwood.patch`: export from a tree without
these patches applied (or revert them first), or the change ends up in both places.
