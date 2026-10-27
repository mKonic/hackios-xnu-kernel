/* Lock order: VFS source link lock, rm_rename_lock, then rm_lock.
 * Keep rm_rename_lock through both identity updates so a later rename cannot
 * have its identity overwritten by this one. Drop rm_lock first: identity
 * updates may release a parent vnode and reenter ramfs_reclaim. */

static int
ramfs_rename(struct vnop_rename_args *ap)
{
	struct ramfs_node *fdir = RAMFS_NODE(ap->a_fdvp);
	struct ramfs_node *tdir = RAMFS_NODE(ap->a_tdvp);
	struct ramfs_node *node = RAMFS_NODE(ap->a_fvp);
	struct ramfs_mount *rm = node->rn_mount;
	struct componentname *fcnp = ap->a_fcnp, *tcnp = ap->a_tcnp;
	struct ramfs_dirent *fde, *tde, *spare;
	int error = 0;

	if (tcnp->cn_namelen > RAMFS_MAX_NAME) {
		return ENAMETOOLONG;
	}
	spare = ramfs_dirent_alloc(tcnp->cn_nameptr, (size_t)tcnp->cn_namelen);

	lck_mtx_lock(&rm->rm_rename_lock);
	lck_mtx_lock(&rm->rm_lock);
	fde = ramfs_dir_find(fdir, fcnp->cn_nameptr, (size_t)fcnp->cn_namelen);
	tde = ramfs_dir_find(tdir, tcnp->cn_nameptr, (size_t)tcnp->cn_namelen);

	if (fde == NULL || fde->rd_node != node || tdir->rn_nlink == 0) {
		error = ENOENT;
		goto out;
	}
	if (node->rn_type == VDIR) {
		/* a directory cannot move into itself or below itself */
		for (struct ramfs_node *up = tdir; up != NULL; up = (up == rm->rm_root) ? NULL : up->rn_parent) {
			if (up == node) {
				error = EINVAL;
				goto out;
			}
		}
	}
	if (tde != NULL && tde->rd_node == node) {
		/* two names of the same file: POSIX wants the rename to do nothing */
		goto out;
	}

	if (tde != NULL) {
		struct ramfs_node *victim = tde->rd_node;

		if (victim->rn_type == VDIR) {
			if (node->rn_type != VDIR) {
				error = EISDIR;
			} else if (victim->rn_nentries != 0) {
				error = ENOTEMPTY;
			}
		} else if (node->rn_type == VDIR) {
			error = ENOTDIR;
		}
		if (error) {
			goto out;
		}
		if (victim->rn_type == VDIR) {
			victim->rn_nlink = 0;
			victim->rn_parent = NULL;
			tdir->rn_nlink--;
		} else {
			victim->rn_nlink--;
			ramfs_touch(victim, false);
		}
		tde->rd_node = node;
		ramfs_touch(tdir, true);
	} else {
		ramfs_dir_add(tdir, spare, node);
		spare = NULL;
	}
	ramfs_dir_remove(fdir, fde);
	if (node->rn_type == VDIR && fdir != tdir) {
		fdir->rn_nlink--;
		tdir->rn_nlink++;
		node->rn_parent = tdir;
	}
	ramfs_touch(node, false);
out:
	lck_mtx_unlock(&rm->rm_lock);
	if (spare != NULL) {
		ramfs_dirent_free(spare);
	}
	if (!error) {
		vnode_update_identity(ap->a_fvp, ap->a_tdvp, tcnp->cn_nameptr, tcnp->cn_namelen, tcnp->cn_hash,
		    VNODE_UPDATE_PARENT | VNODE_UPDATE_NAME);
	}
	lck_mtx_unlock(&rm->rm_rename_lock);
	return error;
}

static int
ramfs_renamex(struct vnop_renamex_args *ap)
{
	/* VNOP_RENAMEX can also be called directly with no flags. */
	if (ap->a_flags == 0) {
		struct vnop_rename_args plain = {
			.a_fdvp = ap->a_fdvp, .a_fvp = ap->a_fvp, .a_fcnp = ap->a_fcnp,
			.a_tdvp = ap->a_tdvp, .a_tvp = ap->a_tvp, .a_tcnp = ap->a_tcnp,
			.a_context = ap->a_context,
		};
		return ramfs_rename(&plain);
	}
	if (ap->a_flags != VFS_RENAME_SWAP) {
		return ENOTSUP;
	}
	if (ap->a_tvp == NULL) {
		return ENOENT;
	}

	struct ramfs_node *fdir = RAMFS_NODE(ap->a_fdvp);
	struct ramfs_node *tdir = RAMFS_NODE(ap->a_tdvp);
	struct ramfs_node *from = RAMFS_NODE(ap->a_fvp);
	struct ramfs_node *to = RAMFS_NODE(ap->a_tvp);
	struct ramfs_mount *rm = from->rn_mount;
	struct componentname *fcnp = ap->a_fcnp, *tcnp = ap->a_tcnp;
	struct ramfs_dirent *fde, *tde;
	int error = 0;
	bool changed = false;

	/* Directory swaps also require ancestor checks and parent link accounting. */
	if (from->rn_type == VDIR || to->rn_type == VDIR) {
		return ENOTSUP;
	}
	lck_mtx_lock(&rm->rm_rename_lock);
	lck_mtx_lock(&rm->rm_lock);
	fde = ramfs_dir_find(fdir, fcnp->cn_nameptr, (size_t)fcnp->cn_namelen);
	tde = ramfs_dir_find(tdir, tcnp->cn_nameptr, (size_t)tcnp->cn_namelen);
	/* Lookups release rm_lock; either name may now refer to another node. */
	if (fde == NULL || tde == NULL || fde->rd_node != from || tde->rd_node != to ||
	    fdir->rn_nlink == 0 || tdir->rn_nlink == 0) {
		error = ENOENT;
	} else if (from != to) {
		/* Both links remain present, including any other hard links to these nodes. */
		fde->rd_node = to;
		tde->rd_node = from;
		ramfs_touch(fdir, true);
		if (fdir != tdir) {
			ramfs_touch(tdir, true);
		}
		ramfs_touch(from, false);
		ramfs_touch(to, false);
		changed = true;
	}
	lck_mtx_unlock(&rm->rm_lock);
	if (changed) {
		vnode_update_identity(ap->a_fvp, ap->a_tdvp, tcnp->cn_nameptr, tcnp->cn_namelen, tcnp->cn_hash,
		    VNODE_UPDATE_PARENT | VNODE_UPDATE_NAME);
		vnode_update_identity(ap->a_tvp, ap->a_fdvp, fcnp->cn_nameptr, fcnp->cn_namelen, fcnp->cn_hash,
		    VNODE_UPDATE_PARENT | VNODE_UPDATE_NAME);
	}
	lck_mtx_unlock(&rm->rm_rename_lock);
	return error;
}
