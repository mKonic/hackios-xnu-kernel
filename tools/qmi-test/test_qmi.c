/*
 * Tests of the QMI helpers (bsd/dev/redwood_qmi.c in the kernel tree) on the machine that builds
 * them; tools/qmi-test/run.sh compiles and runs this.
 *
 * Three kinds:
 *   1. the coding against byte sequences written out by hand from the format's public description
 *      (a header of 7 bytes, then type-length-value triples), with tables of our own that have
 *      every kind of element, and with ath11k's own tables for the messages of its "WLFW" service;
 *   2. the coding against itself and against what ath11k's header says: every one of ath11k's 36
 *      tables filled at random, encoded, decoded and compared; filled to the brim and the length
 *      compared with the ..._MAX_LEN its header gives; a hundred thousand damaged messages decoded
 *      under the address sanitizer;
 *   3. the helpers over a router made up here (struct redwood_qrtr_ops): lookup, a server coming
 *      and going, requests and replies as transactions (in time, late, never, crossed, undecodable),
 *      indications to handlers, and the whole exchange a WCN6750's firmware and ath11k have up to
 *      "firmware ready", played by a firmware made up here.
 * With -DNO_ATH11K (no copy of ath11k's sources at hand) only what needs no table of ath11k runs.
 */

#include "test_qmi.h"

int checks, failures;

void
check(bool ok, const char *format, ...)
{
	va_list list;

	checks++;
	if (ok) {
		return;
	}
	failures++;
	printf("FAIL: ");
	va_start(list, format);
	vprintf(format, list);
	va_end(list);
	printf("\n");
}

void
check_bytes(const uint8_t *got, int got_length, const uint8_t *want, size_t want_length, const char *what)
{
	bool same = got_length == (int)want_length && memcmp(got, want, want_length) == 0;

	check(same, "%s: the encoded message is not the expected one", what);
	if (!same) {
		printf("  got  (%d):", got_length);
		for (int i = 0; i < got_length && i < 80; i++) {
			printf(" %02x", got[i]);
		}
		printf("\n  want (%zu):", want_length);
		for (size_t i = 0; i < want_length && i < 80; i++) {
			printf(" %02x", want[i]);
		}
		printf("\n");
	}
}

uint64_t
now_ms(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000 + (uint64_t)ts.tv_nsec / 1000000;
}

int
main(void)
{
	static const struct {
		const char *name;
		void (*run)(void);
	} parts[] = {
		{ "the coding, tables of our own, bytes by hand", test_own_tables },
#ifndef NO_ATH11K
		{ "the coding, ath11k's messages, bytes by hand", test_wlfw_by_hand },
		{ "the coding, every table of ath11k: full, at random, damaged", test_ath11k_tables },
#endif
		{ "the helpers over a made-up router", test_helpers },
#ifndef NO_ATH11K
		{ "a WCN6750's exchange with ath11k up to \"firmware ready\"", test_conversation },
#endif
	};

	setvbuf(stdout, NULL, _IOLBF, 0);
	for (unsigned i = 0; i < sizeof(parts) / sizeof(parts[0]); i++) {
		int before = failures, done = checks;

		parts[i].run();
		printf("%s  %s (%d checks)\n", failures == before ? "ok  " : "FAIL", parts[i].name, checks - done);
	}
	printf("%d checks, %d failed\n", checks, failures);
	return failures != 0;
}
