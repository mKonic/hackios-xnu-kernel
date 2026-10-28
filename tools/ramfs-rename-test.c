#include "mini.h"

/* Exercises namespace and open-file semantics through XNU's renameatx_np ABI.
 * All writes are confined to a directory created by this invocation. */
#define ROOT "/private/var/tmp/ramfs-rename-test"
#define A ROOT "/a"
#define B ROOT "/b"
#define C ROOT "/sub/c"
#define ALIAS ROOT "/alias"
#define SYM ROOT "/sym"
#define MISSING ROOT "/missing"
#define DIR ROOT "/sub"
enum { RENAMEATX_SYSCALL = 488, OPEN_DPROTECTED_SYSCALL = 216, SWAP = 2 };
static unsigned checks, failures;

static void check(int passed, const char *description)
{
    checks++;
    if (!passed)
        failures++;
    printf("RAMFS_RENAME %s: %s\n", passed ? "PASS" : "FAIL", description);
}

static int rename_flags(const char *from, const char *to, unsigned flags)
{
    return (int)mini_syscall(RENAMEATX_SYSCALL, -2, (long)from, -2, (long)to, flags, 0);
}

static int create(const char *path, const char *value)
{
    int fd = (int)mini_syscall(OPEN_DPROTECTED_SYSCALL, (long)path,
        O_CREAT | O_EXCL | O_RDWR, 4, 0, 0600, 0);
    if (fd < 0)
        return -1;
    size_t n = strlen(value);
    if (write(fd, value, n) != (ssize_t)n || fsync(fd) != 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static int descriptor_equals(int fd, const char *value)
{
    char data[32] = {0};
    ssize_t n = pread(fd, data, sizeof(data), 0);
    return n == (ssize_t)strlen(value) && memcmp(data, value, (size_t)n) == 0;
}

static int path_equals(const char *path, const char *value)
{
    int fd = open(path, O_RDONLY, 0);
    if (fd < 0)
        return 0;
    int equal = descriptor_equals(fd, value);
    close(fd);
    return equal;
}

static int inode_equals(const char *path, uint64_t inode, unsigned links)
{
    struct stat st;
    return lstat(path, &st) == 0 && st.st_ino == inode && st.st_nlink == links;
}

int main(int argc, char **argv, char **envp)
{
    (void)argc; (void)argv; (void)envp;
    int fa = -1, fb = -1, fc = -1;
    struct stat sa, sb, sc;
    if (mkdir(ROOT, 0700) != 0) {
        printf("RAMFS_RENAME setup mkdir errno=%d; existing directory retained\n", errno);
        return 1;
    }
    if (mkdir(DIR, 0700) != 0 || (fa = create(A, "ALPHA")) < 0 ||
        (fb = create(B, "BRAVO")) < 0 || (fc = create(C, "CHARLIE")) < 0 ||
        fstat(fa, &sa) != 0 || fstat(fb, &sb) != 0 || fstat(fc, &sc) != 0) {
        printf("RAMFS_RENAME setup failed errno=%d\n", errno);
        failures++;
        goto cleanup;
    }

    errno = 0;
    int result = rename_flags(A, B, SWAP);
    printf("RAMFS_RENAME swap result=%d errno=%d\n", result, errno);
    check(result == 0, "same-directory swap succeeds");
    check(inode_equals(A, sb.st_ino, 1) && inode_equals(B, sa.st_ino, 1),
        "same-directory names exchange inode identities without changing link counts");
    check(path_equals(A, "BRAVO") && path_equals(B, "ALPHA"), "names expose exchanged contents");
    check(descriptor_equals(fa, "ALPHA") && descriptor_equals(fb, "BRAVO"),
        "open descriptors retain their original files");
    if (result != 0)
        goto cleanup;

    check(rename_flags(A, A, SWAP) == 0 && inode_equals(A, sb.st_ino, 1),
        "same-path swap is a no-op");
    check(link(A, ALIAS) == 0, "hard link setup");
    check(rename_flags(A, ALIAS, SWAP) == 0 && inode_equals(A, sb.st_ino, 2) &&
        inode_equals(ALIAS, sb.st_ino, 2), "two hard links to one inode remain unchanged");
    check(rename_flags(A, C, SWAP) == 0, "cross-directory swap succeeds");
    check(inode_equals(A, sc.st_ino, 1) && inode_equals(C, sb.st_ino, 2) &&
        inode_equals(ALIAS, sb.st_ino, 2), "cross-directory swap preserves other hard links");
    check(path_equals(A, "CHARLIE") && path_equals(C, "BRAVO"),
        "cross-directory paths resolve exchanged contents");
    check(descriptor_equals(fc, "CHARLIE") && descriptor_equals(fb, "BRAVO"),
        "cross-directory swap preserves open descriptors");

    check(symlink("target", SYM) == 0, "symlink setup");
    check(rename_flags(SYM, B, SWAP) == 0, "symlink and regular file swap succeeds");
    char target[16] = {0};
    check(readlink(B, target, sizeof(target)) == 6 && memcmp(target, "target", 6) == 0 &&
        path_equals(SYM, "ALPHA"), "symlink is exchanged without following its target");

    errno = 0;
    result = rename_flags(A, MISSING, SWAP);
    int error = errno;
    check(result < 0 && error == 2 && inode_equals(A, sc.st_ino, 1),
        "missing swap target returns ENOENT without changing source");
    errno = 0;
    result = rename_flags(A, DIR, SWAP);
    error = errno;
    check(result < 0 && error == 45 && inode_equals(A, sc.st_ino, 1),
        "directory swap returns ENOTSUP without changing source");

    check(rename_flags(A, MISSING, 0) == 0 && inode_equals(MISSING, sc.st_ino, 1),
        "zero flags retain ordinary rename behavior");
    check(rename_flags(MISSING, SYM, 0) == 0 && path_equals(SYM, "CHARLIE") &&
        descriptor_equals(fa, "ALPHA"), "ordinary replacement preserves an open unlinked file");
cleanup:
    if (fa >= 0) close(fa);
    if (fb >= 0) close(fb);
    if (fc >= 0) close(fc);
    unlink(A); unlink(B); unlink(C); unlink(ALIAS); unlink(SYM); unlink(MISSING);
    check(rmdir(DIR) == 0 && rmdir(ROOT) == 0, "temporary directories cleaned");
    printf("RAMFS_RENAME_RESULT checks=%u failures=%u\n", checks, failures);
    return failures != 0;
}
