/*
 * Virtio block devices: disk images of the host become /dev/rvblkN (and the character
 * devices /dev/rrvblkN) in the guest, over virtio-pci or virtio-mmio (sys/redwood_virtio.h).
 * See bsd/dev/redwood_virtio_blk.c.
 */
#ifndef _SYS_REDWOOD_VIRTIO_BLK_H_
#define _SYS_REDWOOD_VIRTIO_BLK_H_

#include <stdbool.h>
#include <sys/types.h>
#include <sys/cdefs.h>

__BEGIN_DECLS
void redwood_virtio_blk_init(void);       /* idempotent */
bool redwood_virtio_blk_dev(dev_t dev);    /* one of its devices */
/* rd=rvblkN: the block device of that disk (initializing the driver), or an errno */
int redwood_virtio_blk_rootdev(const char *name, dev_t *dev);
/* /dev/NAME and /dev/rNAME for disk rvblkN as well */
int redwood_virtio_blk_alias(const char *disk, const char *name);
__END_DECLS

#endif /* _SYS_REDWOOD_VIRTIO_BLK_H_ */
