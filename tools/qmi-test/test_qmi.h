/*
 * test_qmi.h -- what the pieces of the QMI helper tests share: the kernel headers under test
 * and the shim Linux ones, the counters and the check helpers (test_qmi.c defines them), and
 * the five suites' entry points (test_encode.c, test_tables.c, test_router.c, test_wcn.c).
 * run.sh compiles all pieces into one program; NO_ATH11K says the ath11k pieces are not built.
 */

#ifndef TEST_QMI_H
#define TEST_QMI_H

#include <errno.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <sys/redwood_qmi.h>
#include <sys/redwood_qrtr.h>

#define HZ 250                          /* what a Linux might have: qmi_txn_wait() must take it from here */
#define msecs_to_jiffies(ms)    ((unsigned long)(ms) * HZ / 1000)

#ifndef NO_ATH11K
#include "qmi.h"                        /* ath11k's, as it is */
#endif

extern int checks, failures;

void check(bool ok, const char *format, ...);
void check_bytes(const uint8_t *got, int got_length, const uint8_t *want, size_t want_length, const char *what);
uint64_t now_ms(void);

void test_own_tables(void);             /* test_encode.c  -- the coding against bytes written by hand */
void test_wlfw_by_hand(void);           /* test_encode.c  -- ath11k's messages, by hand */
void test_ath11k_tables(void);          /* test_tables.c  -- the coding against itself, with every table */
void test_helpers(void);                /* test_router.c  -- the helpers over a router made up here */
void test_conversation(void);           /* test_wcn.c     -- a WCN6750's exchange, both sides played */

#endif /* TEST_QMI_H */
