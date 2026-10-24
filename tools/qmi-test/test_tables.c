/*
 * The coding against itself: every table of ath11k, full, at random, damaged.  Its tables are
 * taken out of qmi.c by extract.py and used by nobody else.  See test_qmi.h for what is shared;
 * run.sh builds the pieces into one program.
 */

#include "test_qmi.h"

#ifndef NO_ATH11K

#include "tables.inc"

/* ---- 2. the coding against itself, with every table of ath11k -------------------------------------- */

static uint64_t seed = 0x9e3779b97f4a7c15ULL;

static uint64_t
rnd(void)
{
	seed ^= seed << 13;
	seed ^= seed >> 7;
	seed ^= seed << 17;
	return seed;
}

static void
put_native(uint8_t *to, uint64_t value, unsigned size)
{
	memcpy(to, &value, size);       /* this machine is little-endian, as the phone is */
}

static uint64_t
get_native(const uint8_t *from, unsigned size)
{
	uint64_t value = 0;

	memcpy(&value, from, size);
	return value;
}

enum fill { FILL_MOST, FILL_RANDOM };

/* Fill the structure a table describes; the table is walked the way the format's description walks it. */
static void
fill(const struct qmi_elem_info *ei, uint8_t *s, bool top, enum fill how)
{
	uint32_t count = 1;

	for (const struct qmi_elem_info *e = ei; e->data_type != QMI_EOTI; e++) {
		if (e->data_type == QMI_OPT_FLAG) {
			s[e->offset] = how == FILL_MOST || (rnd() & 1);
			if (!s[e->offset]) {
				while (e[1].data_type != QMI_EOTI && e[1].tlv_type == e->tlv_type && top) {
					e++;
				}
			}
			continue;
		}
		if (e->data_type == QMI_DATA_LEN) {
			count = how == FILL_MOST ? e[1].elem_len : (uint32_t)(rnd() % (e[1].elem_len + 1));
			put_native(s + e->offset, count, 4);
			continue;
		}
		if (e->array_type == NO_ARRAY) {
			count = 1;
		} else if (e->array_type == STATIC_ARRAY) {
			count = e->elem_len;
		}
		for (uint32_t i = 0; i < count; i++) {
			uint8_t *at = s + e->offset + (size_t)i * e->elem_size;

			if (e->data_type == QMI_STRUCT) {
				fill(e->ei_array, at, false, how);
			} else if (e->data_type == QMI_STRING) {
				size_t length = how == FILL_MOST ? e->elem_len - 1 : rnd() % e->elem_len;

				for (size_t k = 0; k < length; k++) {
					at[k] = (uint8_t)('a' + rnd() % 26);
				}
				at[length] = 0;
			} else {
				put_native(at, rnd(), e->elem_size);
			}
		}
	}
}

/* Do two structures say the same, as far as the table makes them say anything? */
static bool
same(const struct qmi_elem_info *ei, const uint8_t *a, const uint8_t *b, bool top)
{
	uint32_t count = 1;

	for (const struct qmi_elem_info *e = ei; e->data_type != QMI_EOTI; e++) {
		if (e->data_type == QMI_OPT_FLAG) {
			if (!a[e->offset] != !b[e->offset]) {
				return false;
			}
			if (!a[e->offset]) {
				while (e[1].data_type != QMI_EOTI && e[1].tlv_type == e->tlv_type && top) {
					e++;
				}
			}
			continue;
		}
		if (e->data_type == QMI_DATA_LEN) {
			count = (uint32_t)get_native(a + e->offset, 4);
			if (count != get_native(b + e->offset, 4)) {
				return false;
			}
			continue;
		}
		if (e->array_type == NO_ARRAY) {
			count = 1;
		} else if (e->array_type == STATIC_ARRAY) {
			count = e->elem_len;
		}
		for (uint32_t i = 0; i < count; i++) {
			const uint8_t *x = a + e->offset + (size_t)i * e->elem_size, *y = b + e->offset + (size_t)i * e->elem_size;

			if (e->data_type == QMI_STRUCT ? !same(e->ei_array, x, y, false) : e->data_type == QMI_STRING ?
			    strcmp((const char *)x, (const char *)y) != 0 : memcmp(x, y, e->elem_size) != 0) {
				return false;
			}
		}
	}
	return true;
}

/* What a table is expected to be like: said once, so that a table of another kind does not go unnoticed. */
static void
check_table(const struct test_message *m, const struct qmi_elem_info *ei, size_t size, bool top)
{
	for (const struct qmi_elem_info *e = ei; e->data_type != QMI_EOTI; e++) {
		check(e - ei < 100, "%s: a table without an end", m->name);
		check(top || e->data_type != QMI_OPT_FLAG, "%s: an optional element inside a structure", m->name);
		check(e->data_type != QMI_DATA_LEN || (e[1].array_type == VAR_LEN_ARRAY && (e->elem_size == 1 || e->elem_size == 2) &&
		    e->offset + 4 <= size), "%s: a count that is not a 32-bit field before a variable array", m->name);
		check(e->array_type != VAR_LEN_ARRAY || (e > ei && e[-1].data_type == QMI_DATA_LEN), "%s: a variable array without a count", m->name);
		check(e->data_type == QMI_OPT_FLAG || e->data_type == QMI_DATA_LEN ||
		    e->offset + (size_t)e->elem_size * (e->data_type == QMI_STRING || e->array_type == NO_ARRAY ? 1 : e->elem_len) *
		    (e->data_type == QMI_STRING ? e->elem_len : 1) <= size, "%s: an element beyond the end of its structure", m->name);
		if (e->data_type == QMI_STRUCT) {
			check_table(m, e->ei_array, e->elem_size, false);
		}
	}
}

void
test_ath11k_tables(void)
{
	static uint8_t message[16384], again[16384], damaged[16384];
	unsigned with_limit = 0, exact = 0, messages = 0;

	for (unsigned t = 0; t < sizeof(ath11k_messages) / sizeof(ath11k_messages[0]); t++) {
		const struct test_message *m = &ath11k_messages[t];
		uint8_t *a = calloc(1, m->size + 16), *b = calloc(1, m->size + 16);
		int length, second;

		check_table(m, m->ei, m->size, true);
		if (!m->message) {
			free(a);
			free(b);
			continue;               /* a structure inside a message: gone through with the messages that have it */
		}
		messages++;

		/* filled to the brim: as long as ath11k's header says the message can get */
		fill(m->ei, a, true, FILL_MOST);
		length = redwood_qmi_encode(QMI_REQUEST, 1, 1, m->ei, a, message, sizeof(message));
		check(length >= QMI_HEADER_SIZE, "%s: cannot be encoded when full (%d)", m->name, length);
		if (m->limit >= 0) {
			/*
			 * Two of ath11k's tables say less than the interface they are of, and its header's figure
			 * is the interface's: "rejuvenate_enable" is a 32-bit number there and in the C structure
			 * and one byte in the table (3 bytes less), and the host capabilities' table has 14 of the
			 * interface's elements (72 bytes less). What counts on the wire is the table, for Linux too.
			 */
			int expected = strcmp(m->name, "qmi_wlanfw_ind_register_req_msg_v01") == 0 ? m->limit - 3 :
			    strcmp(m->name, "qmi_wlanfw_host_cap_req_msg_v01") == 0 ? m->limit - 72 : m->limit;

			with_limit++;
			exact += expected == m->limit;
			check(length - QMI_HEADER_SIZE == expected, "%s: full, it encodes to %d bytes, not %d (ath11k's header says %d at most)",
			    m->name, length - QMI_HEADER_SIZE, expected, m->limit);
		}
		check(redwood_qmi_decode(message, (size_t)length, m->ei, b) == length && same(m->ei, a, b, true), "%s: full, it does not come back the same",
		    m->name);
		/* (the bytes after a structure are guard bytes: nothing may have been written there) */
		check(memcmp(b + m->size, "\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0", 16) == 0, "%s: decoding wrote past the structure", m->name);

		/* at random, 300 times: encode, decode, compare; encode what was decoded, compare the bytes */
		for (int round = 0; round < 300; round++) {
			memset(a, 0, m->size);
			memset(b, 0, m->size);
			fill(m->ei, a, true, FILL_RANDOM);
			length = redwood_qmi_encode(QMI_INDICATION, (uint16_t)rnd(), (uint16_t)rnd(), m->ei, a, message, sizeof(message));
			if (length < 0 || redwood_qmi_decode(message, (size_t)length, m->ei, b) != length || !same(m->ei, a, b, true)) {
				check(false, "%s: round %d: does not come back the same (%d)", m->name, round, length);
				break;
			}
			second = redwood_qmi_encode(message[0], (uint16_t)(message[3] | message[4] << 8), (uint16_t)(message[1] | message[2] << 8),
			    m->ei, b, again, sizeof(again));
			if (second != length || memcmp(message, again, (size_t)length) != 0) {
				check(false, "%s: round %d: encoded again, the bytes differ", m->name, round);
				break;
			}
			/* and the same message damaged: it may be refused, it may not be read or written out of bounds */
			for (int k = 0; k < 10 && length > QMI_HEADER_SIZE; k++) {
				size_t cut = (size_t)length;

				memcpy(damaged, message, (size_t)length);
				switch (rnd() % 4) {
				case 0: damaged[QMI_HEADER_SIZE + rnd() % (size_t)(length - QMI_HEADER_SIZE)] ^= (uint8_t)(1u << (rnd() % 8)); break;
				case 1: damaged[QMI_HEADER_SIZE + rnd() % (size_t)(length - QMI_HEADER_SIZE)] = (uint8_t)rnd(); break;
				case 2: cut = QMI_HEADER_SIZE + rnd() % (size_t)(length - QMI_HEADER_SIZE);
					damaged[5] = (uint8_t)(cut - QMI_HEADER_SIZE); damaged[6] = (uint8_t)((cut - QMI_HEADER_SIZE) >> 8); break;
				default: damaged[5] = (uint8_t)rnd(); damaged[6] = (uint8_t)rnd(); break;
				}
				memset(b, 0, m->size);
				{
					/* exactly as much memory as the message is long, so that the sanitizer sees a read past it */
					uint8_t *exact = malloc(cut ? cut : 1);

					memcpy(exact, damaged, cut);
					(void)redwood_qmi_decode(exact, cut, m->ei, b);
					free(exact);
				}
				checks++;
			}
		}
		check(memcmp(b + m->size, "\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0", 16) == 0, "%s: decoding a damaged message wrote past the structure", m->name);
		free(a);
		free(b);
	}
	printf("  ath11k's tables: %zu, of them %u messages; %u have a largest length in ath11k's header, %u of them met to the byte\n",
	    sizeof(ath11k_messages) / sizeof(ath11k_messages[0]), messages, with_limit, exact);
}

#endif /* !NO_ATH11K */
