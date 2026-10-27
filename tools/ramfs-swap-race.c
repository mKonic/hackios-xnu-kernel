#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/mount.h>
#include <time.h>
#include <unistd.h>

/* Workers are idle during validation; path mismatches cannot be attributed to
 * a simultaneous rename. Same-direction calls can hold different source-vnode
 * link locks after the first call has exchanged the directory entries. */
extern int renameatx_np(int, const char *, int, const char *, unsigned);
enum { SWAP = 2 };
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cv = PTHREAD_COND_INITIALIZER;
static unsigned generation, ready, done, batch;
static int stop, opposite;
static char paths[2][PATH_MAX];
struct worker { unsigned id; uint64_t ok, missing, other; int first_error; };

static void *run(void *arg)
{
    struct worker *w = arg;
    unsigned seen = 0;
    pthread_mutex_lock(&lock);
    ready++;
    pthread_cond_broadcast(&cv);
    for (;;) {
        while (!stop && generation == seen)
            pthread_cond_wait(&cv, &lock);
        if (stop) break;
        seen = generation;
        unsigned source = opposite ? w->id : 0;
        pthread_mutex_unlock(&lock);
        for (unsigned i = 0; i < batch; i++) {
            if (renameatx_np(AT_FDCWD, paths[source], AT_FDCWD,
                            paths[1 - source], SWAP) == 0) {
                w->ok++;
            } else if (errno == ENOENT) {
                w->missing++;
            } else {
                if (!w->other) w->first_error = errno;
                w->other++;
            }
        }
        pthread_mutex_lock(&lock);
        done++;
        pthread_cond_broadcast(&cv);
    }
    pthread_mutex_unlock(&lock);
    return NULL;
}

static int same_inode(const struct stat *a, const struct stat *b)
{
    return a->st_dev == b->st_dev && a->st_ino == b->st_ino;
}

static int validate(const char *mode, unsigned round, int fd[2], struct stat identity[2])
{
    char reported[2][PATH_MAX] = {{0}}, contents[8];
    struct stat actual[2], named[2], now;
    int bad = 0, pathok[2];
    /* Obtain both cached paths before stat/open could affect vnode identity. */
    for (unsigned i = 0; i < 2; i++) {
        errno = 0;
        pathok[i] = fcntl(fd[i], F_GETPATH, reported[i]) == 0;
        if (!pathok[i]) {
            printf("RAMFS_RACE GETPATH mode=%s round=%u fd=%u errno=%d\n",
                   mode, round, i, errno);
            bad = 1;
        }
    }
    for (unsigned i = 0; i < 2; i++) {
        if (pathok[i] && (stat(reported[i], &actual[i]) != 0 ||
                         !same_inode(&actual[i], &identity[i]))) {
            printf("RAMFS_RACE IDENTITY mode=%s round=%u fd=%u inode=%llu path=%s\n",
                   mode, round, i, (unsigned long long)identity[i].st_ino, reported[i]);
            bad = 1;
        }
        if (fstat(fd[i], &now) != 0 || !same_inode(&now, &identity[i]) ||
            now.st_nlink != 1 || pread(fd[i], contents, sizeof(contents), 0) != 1 ||
            contents[0] != (char)('X' + i)) {
            printf("RAMFS_RACE DESCRIPTOR mode=%s round=%u fd=%u\n", mode, round, i);
            bad = 1;
        }
    }
    if (stat(paths[0], &named[0]) != 0 || stat(paths[1], &named[1]) != 0 ||
        !((same_inode(&named[0], &identity[0]) && same_inode(&named[1], &identity[1])) ||
          (same_inode(&named[0], &identity[1]) && same_inode(&named[1], &identity[0])))) {
        printf("RAMFS_RACE NAMESPACE mode=%s round=%u\n", mode, round);
        bad = 1;
    }
    if (bad) fflush(stdout);
    return bad;
}

static void wait_count(unsigned *value, unsigned target, const char *mode, unsigned round)
{
    struct timespec deadline;
    clock_gettime(CLOCK_REALTIME, &deadline);
    deadline.tv_sec += 30;
    while (*value != target) {
        int error = pthread_cond_timedwait(&cv, &lock, &deadline);
        if (error) {
            printf("RAMFS_RACE TIMEOUT mode=%s round=%u completed=%u error=%d\n",
                   mode, round, *value, error);
            fflush(stdout);
            _exit(3);
        }
    }
}

static int scenario(const char *root, int crossdir, int reverse, unsigned rounds)
{
    char sub[PATH_MAX], mode[64];
    int fd[2] = {-1, -1}, bad = 0;
    struct stat identity[2];
    struct worker workers[2] = {{.id = 0}, {.id = 1}};
    pthread_t threads[2];
    unsigned created = 0, completed = 0;
    snprintf(sub, sizeof(sub), "%s/sub", root);
    snprintf(paths[0], sizeof(paths[0]), "%s/a", root);
    snprintf(paths[1], sizeof(paths[1]), "%s/%sb", root, crossdir ? "sub/" : "");
    snprintf(mode, sizeof(mode), "%s-%s", crossdir ? "cross" : "same",
             reverse ? "opposite" : "parallel");
    printf("RAMFS_RACE BEGIN mode=%s rounds=%u batch=%u\n", mode, rounds, batch);
    fflush(stdout);
    if (mkdir(sub, 0700) != 0) { bad = 1; goto cleanup; }
    for (unsigned i = 0; i < 2; i++) {
        char value = (char)('X' + i);
        fd[i] = open(paths[i], O_CREAT | O_EXCL | O_RDWR, 0600);
        if (fd[i] < 0 || write(fd[i], &value, 1) != 1 || fstat(fd[i], &identity[i])) {
            bad = 1; goto cleanup;
        }
    }
    if (validate(mode, 0, fd, identity) ||
        renameatx_np(AT_FDCWD, paths[0], AT_FDCWD, paths[1], SWAP) ||
        validate(mode, 0, fd, identity)) { bad = 1; goto cleanup; }
    generation = ready = done = 0;
    stop = 0;
    opposite = reverse;
    for (; created < 2; created++) {
        int e = pthread_create(&threads[created], NULL, run, &workers[created]);
        if (e) { errno = e; bad = 1; break; }
    }
    if (!bad) {
        pthread_mutex_lock(&lock);
        wait_count(&ready, 2, mode, 0);
        pthread_mutex_unlock(&lock);
        for (unsigned round = 1; round <= rounds; round++) {
            pthread_mutex_lock(&lock);
            done = 0;
            generation++;
            pthread_cond_broadcast(&cv);
            wait_count(&done, 2, mode, round);
            pthread_mutex_unlock(&lock);
            completed = round;
            if (validate(mode, round, fd, identity)) {
                /* Repeat while both workers remain parked to detect persistence. */
                int repeated = validate(mode, round, fd, identity);
                printf("RAMFS_RACE PERSISTENT mode=%s round=%u repeated=%d\n", mode, round, repeated);
                bad = 1;
                break;
            }
        }
    }
    pthread_mutex_lock(&lock);
    stop = 1;
    pthread_cond_broadcast(&cv);
    pthread_mutex_unlock(&lock);
    for (unsigned i = 0; i < created; i++) pthread_join(threads[i], NULL);
    for (unsigned i = 0; i < created; i++) {
        printf("RAMFS_RACE WORKER mode=%s id=%u ok=%llu enoent=%llu other=%llu first_error=%d\n",
               mode, i, (unsigned long long)workers[i].ok,
               (unsigned long long)workers[i].missing,
               (unsigned long long)workers[i].other, workers[i].first_error);
        if (workers[i].other || !workers[i].ok) bad = 1;
    }
cleanup:
    if (bad && !completed) printf("RAMFS_RACE SETUP mode=%s errno=%d\n", mode, errno);
    for (unsigned i = 0; i < 2; i++) {
        if (fd[i] >= 0) { close(fd[i]); if (unlink(paths[i])) bad = 1; }
    }
    if (rmdir(sub)) bad = 1;
    printf("RAMFS_RACE CASE mode=%s rounds=%u batch=%u result=%s\n", mode, completed, batch, bad ? "FAIL" : "PASS");
    fflush(stdout);
    return bad;
}

int main(int argc, char **argv)
{
    struct statfs filesystem;
    if (statfs("/private/var/tmp", &filesystem) != 0) {
        printf("RAMFS_RACE statfs errno=%d\n", errno);
        return 2;
    }
    /* Restrict concurrent SWAP to ramfs; this test may block on APFS. */
    if (strcmp(filesystem.f_fstypename, "ramfs") != 0) {
        printf("RAMFS_RACE requires ramfs; found %s\n", filesystem.f_fstypename);
        return 2;
    }
    unsigned rounds = argc > 1 ? (unsigned)strtoul(argv[1], NULL, 10) : 128;
    batch = argc > 2 ? (unsigned)strtoul(argv[2], NULL, 10) : 32;
    if (!rounds || rounds > 100000 || !batch || batch > 10000) return 2;
    char root[] = "/private/var/tmp/ramfs-swap-race.XXXXXX";
    if (!mkdtemp(root)) { printf("RAMFS_RACE mkdir errno=%d\n", errno); return 2; }
    int failures = 0;
    int selection = argc > 3 ? atoi(argv[3]) : -1;
    for (int cross = 0; cross < 2; cross++)
        for (int reverse = 1; reverse >= 0; reverse--)
            if (selection < 0 || selection == 2 * cross + 1 - reverse)
                failures += scenario(root, cross, reverse, rounds);
    if (rmdir(root)) failures++;
    printf("RAMFS_RACE_RESULT failures=%d rounds=%u batch=%u\n", failures, rounds, batch);
    return failures != 0;
}
