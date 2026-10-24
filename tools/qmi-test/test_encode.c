/*
 * The coding against byte sequences written out by hand, ours and ath11k's messages.
 * See test_qmi.h for what is shared; run.sh builds the pieces into one program.
 */

#include "test_qmi.h"

/* ---- 1. the coding, against bytes written by hand ------------------------------------------------- */

/* A message of our own with every kind of element in it. */
struct inner {
	uint16_t a;
	uint32_t pairs_len;
	uint8_t pairs[4];
	char name[10];                  /* up to 9 characters: a 1-byte length before it in a message */
};
struct outer {
	uint8_t first;                  /* TLV 1 */
	uint8_t second_valid;
	uint32_t second;                /* TLV 0x10 */
	uint8_t list_valid;
	uint32_t list_len;
	uint16_t list[5];               /* TLV 0x11 */
	uint64_t fixed[2];              /* TLV 2: always two */
	uint8_t inners_valid;
	uint32_t inners_len;
	struct inner inners[3];         /* TLV 0x12 */
	uint8_t text_valid;
	char text[300];                 /* TLV 0x13: at the top level, no length before it */
	int16_t kind;                   /* TLV 3 */
	uint8_t big_valid;
	struct {
		char name[300];         /* over 255: a 2-byte length before it */
		uint32_t n;
	} big;                          /* TLV 0x14 */
};

static const struct qmi_elem_info inner_ei[] = {
	{ QMI_UNSIGNED_2_BYTE, 1, sizeof(uint16_t), NO_ARRAY, 0, offsetof(struct inner, a), NULL },
	{ QMI_DATA_LEN, 1, sizeof(uint8_t), NO_ARRAY, 0, offsetof(struct inner, pairs_len), NULL },
	{ QMI_UNSIGNED_1_BYTE, 4, sizeof(uint8_t), VAR_LEN_ARRAY, 0, offsetof(struct inner, pairs), NULL },
	{ QMI_STRING, 10, sizeof(char), NO_ARRAY, 0, offsetof(struct inner, name), NULL },
	{ QMI_EOTI, 0, 0, NO_ARRAY, 0, 0, NULL },
};
static const struct qmi_elem_info big_ei[] = {
	{ QMI_STRING, 300, sizeof(char), NO_ARRAY, 0, 0, NULL },
	{ QMI_UNSIGNED_4_BYTE, 1, sizeof(uint32_t), NO_ARRAY, 0, 300, NULL },
	{ QMI_EOTI, 0, 0, NO_ARRAY, 0, 0, NULL },
};
static const struct qmi_elem_info outer_ei[] = {
	{ QMI_UNSIGNED_1_BYTE, 1, sizeof(uint8_t), NO_ARRAY, 0x01, offsetof(struct outer, first), NULL },
	{ QMI_OPT_FLAG, 1, sizeof(uint8_t), NO_ARRAY, 0x10, offsetof(struct outer, second_valid), NULL },
	{ QMI_UNSIGNED_4_BYTE, 1, sizeof(uint32_t), NO_ARRAY, 0x10, offsetof(struct outer, second), NULL },
	{ QMI_OPT_FLAG, 1, sizeof(uint8_t), NO_ARRAY, 0x11, offsetof(struct outer, list_valid), NULL },
	{ QMI_DATA_LEN, 1, sizeof(uint16_t), NO_ARRAY, 0x11, offsetof(struct outer, list_len), NULL },
	{ QMI_UNSIGNED_2_BYTE, 5, sizeof(uint16_t), VAR_LEN_ARRAY, 0x11, offsetof(struct outer, list), NULL },
	{ QMI_UNSIGNED_8_BYTE, 2, sizeof(uint64_t), STATIC_ARRAY, 0x02, offsetof(struct outer, fixed), NULL },
	{ QMI_OPT_FLAG, 1, sizeof(uint8_t), NO_ARRAY, 0x12, offsetof(struct outer, inners_valid), NULL },
	{ QMI_DATA_LEN, 1, sizeof(uint8_t), NO_ARRAY, 0x12, offsetof(struct outer, inners_len), NULL },
	{ QMI_STRUCT, 3, sizeof(struct inner), VAR_LEN_ARRAY, 0x12, offsetof(struct outer, inners), inner_ei },
	{ QMI_OPT_FLAG, 1, sizeof(uint8_t), NO_ARRAY, 0x13, offsetof(struct outer, text_valid), NULL },
	{ QMI_STRING, 300, sizeof(char), NO_ARRAY, 0x13, offsetof(struct outer, text), NULL },
	{ QMI_SIGNED_2_BYTE_ENUM, 1, sizeof(int16_t), NO_ARRAY, 0x03, offsetof(struct outer, kind), NULL },
	{ QMI_OPT_FLAG, 1, sizeof(uint8_t), NO_ARRAY, 0x14, offsetof(struct outer, big_valid), NULL },
	{ QMI_STRUCT, 1, 304, NO_ARRAY, 0x14, offsetof(struct outer, big), big_ei },
	{ QMI_EOTI, 0, 0, NO_ARRAY, 0, 0, NULL },
};

void
test_own_tables(void)
{
	/* everything there */
	static const uint8_t full[] = {
		0x00, 0x34, 0x12, 0xcd, 0xab, 0x51, 0x00,               /* a request, transaction 0x1234, message 0xabcd, 81 bytes follow */
		0x01, 0x01, 0x00, 0x7f,                                 /* first */
		0x10, 0x04, 0x00, 0x78, 0x56, 0x34, 0x12,               /* second */
		0x11, 0x06, 0x00, 0x02, 0x00, 0x01, 0x00, 0xff, 0xff,   /* list: a 2-byte count, two entries */
		0x02, 0x10, 0x00, 0x08, 0x07, 0x06, 0x05, 0x04, 0x03, 0x02, 0x01, 0, 0, 0, 0, 0, 0, 0, 0x80,    /* fixed */
		0x12, 0x0e, 0x00, 0x02,                                 /* inners: a 1-byte count, two of them */
		0x11, 0x22, 0x03, 0xa1, 0xa2, 0xa3, 0x02, 'h', 'i',     /*   a, a count and three bytes, a length and "hi" */
		0x33, 0x44, 0x00, 0x00,                                 /*   a, no pairs, an empty string */
		0x13, 0x05, 0x00, 'h', 'e', 'l', 'l', 'o',              /* text: the TLV's length is the string's */
		0x03, 0x02, 0x00, 0xfe, 0xff,                           /* kind = -2 */
		0x14, 0x09, 0x00, 0x03, 0x00, 'x', 'y', 'z', 0x2a, 0, 0, 0,     /* big: a 2-byte length, "xyz", n */
	};
	/* nothing optional: only the three that have no flag, in the table's order */
	static const uint8_t bare[] = {
		0x04, 0x00, 0x00, 0x01, 0x00, 0x1c, 0x00,               /* an indication */
		0x01, 0x01, 0x00, 0x00,
		0x02, 0x10, 0x00, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
		0x03, 0x02, 0x00, 0x05, 0x00,
	};
	/* as `full` says it, but the TLVs in another order, one unknown among them, one given twice */
	static const uint8_t shuffled[] = {
		0x02, 0x01, 0x00, 0x02, 0x00, 0x26, 0x00,
		0x03, 0x02, 0x00, 0x09, 0x00,
		0xe0, 0x03, 0x00, 1, 2, 3,                              /* nobody knows 0xe0 */
		0x10, 0x04, 0x00, 1, 0, 0, 0,
		0x05, 0x01, 0x00, 0x99,                                 /* nor 5, which is below 0x10 */
		0x01, 0x01, 0x00, 0x11,
		0x10, 0x04, 0x00, 2, 0, 0, 0,                           /* the later one counts */
		0x11, 0x02, 0x00, 0x00, 0x00,                           /* a list of none */
	};
	struct outer o, back;
	uint8_t buffer[512];
	int n;

	memset(&o, 0, sizeof(o));
	o.first = 0x7f;
	o.second_valid = 1;
	o.second = 0x12345678;
	o.list_valid = 1;
	o.list_len = 2;
	o.list[0] = 1;
	o.list[1] = 0xffff;
	o.fixed[0] = 0x0102030405060708ULL;
	o.fixed[1] = 0x8000000000000000ULL;
	o.inners_valid = 1;
	o.inners_len = 2;
	o.inners[0].a = 0x2211;
	o.inners[0].pairs_len = 3;
	memcpy(o.inners[0].pairs, "\xa1\xa2\xa3", 3);
	strcpy(o.inners[0].name, "hi");
	o.inners[1].a = 0x4433;
	o.text_valid = 1;
	strcpy(o.text, "hello");
	o.kind = -2;
	o.big_valid = 1;
	strcpy(o.big.name, "xyz");
	o.big.n = 42;
	n = redwood_qmi_encode(QMI_REQUEST, 0xabcd, 0x1234, outer_ei, &o, buffer, sizeof(buffer));
	check_bytes(buffer, n, full, sizeof(full), "own table, everything there");

	memset(&back, 0, sizeof(back));
	n = redwood_qmi_decode(full, sizeof(full), outer_ei, &back);
	check(n == (int)sizeof(full), "own table: decoding gives %d, not the message's %zu bytes", n, sizeof(full));
	check(back.first == 0x7f && back.second_valid == 1 && back.second == 0x12345678 && back.list_valid == 1 &&
	    back.list_len == 2 && back.list[0] == 1 && back.list[1] == 0xffff && back.fixed[0] == 0x0102030405060708ULL &&
	    back.fixed[1] == 0x8000000000000000ULL && back.inners_valid == 1 && back.inners_len == 2 &&
	    back.inners[0].a == 0x2211 && back.inners[0].pairs_len == 3 && memcmp(back.inners[0].pairs, "\xa1\xa2\xa3", 3) == 0 &&
	    strcmp(back.inners[0].name, "hi") == 0 && back.inners[1].a == 0x4433 && back.inners[1].pairs_len == 0 &&
	    back.inners[1].name[0] == 0 && back.text_valid == 1 && strcmp(back.text, "hello") == 0 && back.kind == -2 &&
	    back.big_valid == 1 && strcmp(back.big.name, "xyz") == 0 && back.big.n == 42,
	    "own table: the decoded structure is not what the bytes say");

	memset(&o, 0, sizeof(o));
	o.kind = 5;
	o.second = 77;                  /* not valid: must not be sent */
	o.list_len = 3;
	n = redwood_qmi_encode(QMI_INDICATION, 1, 0, outer_ei, &o, buffer, sizeof(buffer));
	check_bytes(buffer, n, bare, sizeof(bare), "own table, nothing optional");

	memset(&back, 0xee, sizeof(back));
	memset(&back, 0, offsetof(struct outer, fixed));
	back.inners_valid = back.text_valid = back.big_valid = 0;
	n = redwood_qmi_decode(shuffled, sizeof(shuffled), outer_ei, &back);
	check(n == (int)sizeof(shuffled) && back.kind == 9 && back.first == 0x11 && back.second_valid == 1 && back.second == 2 &&
	    back.list_valid == 1 && back.list_len == 0 && back.inners_valid == 0 && back.text_valid == 0 && back.big_valid == 0,
	    "own table: TLVs out of order, unknown ones and a repeated one (%d)", n);
	check(back.fixed[0] == 0xeeeeeeeeeeeeeeeeULL, "own table: a TLV that is not in the message was written to");

	/* what must be refused */
	memset(&o, 0, sizeof(o));
	check(redwood_qmi_encode(QMI_REQUEST, 1, 1, outer_ei, &o, buffer, 34) == -EMSGSIZE, "a buffer one byte too small is not refused");
	check(redwood_qmi_encode(QMI_REQUEST, 1, 1, outer_ei, &o, buffer, 35) == 35, "a buffer of exactly the size is refused");
	o.list_valid = 1;
	o.list_len = 6;
	check(redwood_qmi_encode(QMI_REQUEST, 1, 1, outer_ei, &o, buffer, sizeof(buffer)) == -EINVAL, "a count over the array's size is encoded");
	o.list_len = 0;
	o.text_valid = 1;
	memset(o.text, 'a', sizeof(o.text));
	check(redwood_qmi_encode(QMI_REQUEST, 1, 1, outer_ei, &o, buffer, sizeof(buffer)) == -EINVAL, "a string with no end is encoded");
	{
		static const uint8_t count_too_big[] = { 0, 0, 0, 0, 0, 0x0f, 0, 0x11, 0x0c, 0, 0x06, 0, 1, 0, 2, 0, 3, 0, 4, 0, 5, 0 };
		static const uint8_t tlv_past_end[] = { 0, 0, 0, 0, 0, 0x04, 0, 0x01, 0x05, 0, 0x01 };
		static const uint8_t value_past_tlv[] = { 0, 0, 0, 0, 0, 0x05, 0, 0x10, 0x02, 0, 0x01, 0x02 };
		static const uint8_t length_lies[] = { 0, 0, 0, 0, 0, 0x40, 0, 0x01, 0x01, 0, 0x01 };
		static const uint8_t stray_byte[] = { 0, 0, 0, 0, 0, 0x05, 0, 0x01, 0x01, 0, 0x01, 0x10 };
		static const uint8_t inner_short[] = { 0, 0, 0, 0, 0, 0x07, 0, 0x12, 0x04, 0, 0x01, 0x11, 0x22, 0x03 };
		uint8_t long_string[7 + 3 + 300];

		memset(&back, 0, sizeof(back));
		check(redwood_qmi_decode(count_too_big, sizeof(count_too_big), outer_ei, &back) == -EINVAL, "a count over the array's size is decoded");
		check(redwood_qmi_decode(tlv_past_end, sizeof(tlv_past_end), outer_ei, &back) == -EINVAL, "a TLV longer than the message is decoded");
		check(redwood_qmi_decode(value_past_tlv, sizeof(value_past_tlv), outer_ei, &back) == -EINVAL, "a number longer than its TLV is decoded");
		check(redwood_qmi_decode(length_lies, sizeof(length_lies), outer_ei, &back) == -EINVAL, "a header that says more than there is, is believed");
		check(redwood_qmi_decode(stray_byte, sizeof(stray_byte), outer_ei, &back) == -EINVAL, "a stray byte after the last TLV is let pass");
		check(redwood_qmi_decode(inner_short, sizeof(inner_short), outer_ei, &back) == -EINVAL, "a structure cut short is decoded");
		memset(long_string, 'b', sizeof(long_string));
		memcpy(long_string, "\0\0\0\0\0\x2f\x01\x13\x2c\x01", 10);     /* a string of exactly 300 */
		check(redwood_qmi_decode(long_string, sizeof(long_string), outer_ei, &back) == -EINVAL, "a string that fills its buffer is decoded");
		long_string[5] = 0x2e;
		long_string[8] = 0x2b;                                          /* 299: fits, with its zero */
		check(redwood_qmi_decode(long_string, sizeof(long_string) - 1, outer_ei, &back) == 7 + 3 + 299 && strlen(back.text) == 299,
		    "a string one short of its buffer is not decoded");
	}
	/* the reply every response starts with */
	{
		static const uint8_t ok[] = { 0x02, 0x07, 0x00, 0x22, 0x00, 0x07, 0x00, 0x02, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00 };
		static const uint8_t no[] = { 0x02, 0x07, 0x00, 0x22, 0x00, 0x07, 0x00, 0x02, 0x04, 0x00, 0x01, 0x00, 0x5e, 0x00 };
		static const struct qmi_elem_info reply_ei[] = {
			{ QMI_STRUCT, 1, sizeof(struct qmi_response_type_v01), NO_ARRAY, 0x02, 0, qmi_response_type_v01_ei },
			{ QMI_EOTI, 0, 0, NO_ARRAY, 0, 0, NULL },
		};
		struct qmi_response_type_v01 r = { 0xffff, 0xffff };

		check(redwood_qmi_decode(ok, sizeof(ok), reply_ei, &r) == 14 && r.result == QMI_RESULT_SUCCESS_V01 && r.error == QMI_ERR_NONE_V01,
		    "the common reply, success");
		check(redwood_qmi_decode(no, sizeof(no), reply_ei, &r) == 14 && r.result == QMI_RESULT_FAILURE_V01 &&
		    r.error == QMI_ERR_NOT_SUPPORTED_V01, "the common reply, failure");
		check_bytes(buffer, redwood_qmi_encode(QMI_RESPONSE, 0x22, 7, reply_ei, &r, buffer, sizeof(buffer)), no, sizeof(no), "the common reply");
	}
	/* a request with nothing in it */
	{
		static const uint8_t empty[] = { 0x00, 0x09, 0x00, 0x24, 0x00, 0x00, 0x00 };
		static const struct qmi_elem_info none_ei[] = { { QMI_EOTI, 0, 0, NO_ARRAY, 0, 0, NULL } };
		uint8_t nothing = 0;

		check_bytes(buffer, redwood_qmi_encode(QMI_REQUEST, 0x24, 9, none_ei, &nothing, buffer, sizeof(buffer)), empty, sizeof(empty), "an empty request");
		check_bytes(buffer, redwood_qmi_encode(QMI_REQUEST, 0x24, 9, NULL, NULL, buffer, sizeof(buffer)), empty, sizeof(empty), "a request with no table");
	}
}

#ifndef NO_ATH11K

/* ---- ... and ath11k's messages, by hand ------------------------------------------------------------- */

void
test_wlfw_by_hand(void)
{
	uint8_t buffer[8192];

	/* "tell me when these happen": six flags and a client's number */
	{
		static const uint8_t want[] = {
			0x00, 0x05, 0x00, 0x20, 0x00, 0x1b, 0x00,
			0x10, 0x01, 0x00, 0x01,                                 /* fw_ready_enable */
			0x15, 0x04, 0x00, 0x4c, 0x45, 0x4e, 0x4b,               /* client_id */
			0x16, 0x01, 0x00, 0x01,                                 /* request_mem_enable */
			0x17, 0x01, 0x00, 0x01,                                 /* fw_mem_ready_enable */
			0x18, 0x01, 0x00, 0x01,                                 /* fw_init_done_enable */
			0x1b, 0x01, 0x00, 0x01,                                 /* cal_done_enable */
		};
		struct qmi_wlanfw_ind_register_req_msg_v01 req;

		memset(&req, 0, sizeof(req));
		req.client_id_valid = 1;
		req.client_id = 0x4b4e454c;
		req.fw_ready_enable_valid = 1;
		req.fw_ready_enable = 1;
		req.request_mem_enable_valid = 1;
		req.request_mem_enable = 1;
		req.fw_mem_ready_enable_valid = 1;
		req.fw_mem_ready_enable = 1;
		req.fw_init_done_enable_valid = 1;
		req.fw_init_done_enable = 1;
		req.cal_done_enable_valid = 1;
		req.cal_done_enable = 1;
		req.pin_connect_result_enable = 1;      /* (not valid: stays out) */
		check_bytes(buffer, redwood_qmi_encode(QMI_REQUEST, 0x20, 5, qmi_wlanfw_ind_register_req_msg_v01_ei, &req, buffer,
		    sizeof(buffer)), want, sizeof(want), "WLFW indication register request");
	}
	/* the mode: one number that is always there, one byte that may be */
	{
		static const uint8_t want[] = {
			0x00, 0x09, 0x00, 0x22, 0x00, 0x0b, 0x00,
			0x01, 0x04, 0x00, 0x07, 0x00, 0x00, 0x00,               /* mode 7: cold boot calibration */
			0x10, 0x01, 0x00, 0x00,                                 /* hw_debug */
		};
		struct qmi_wlanfw_wlan_mode_req_msg_v01 req = { .mode = 7, .hw_debug_valid = 1, .hw_debug = 0 };
		struct qmi_wlanfw_wlan_mode_req_msg_v01 back;

		check_bytes(buffer, redwood_qmi_encode(QMI_REQUEST, 0x22, 9, qmi_wlanfw_wlan_mode_req_msg_v01_ei, &req, buffer,
		    sizeof(buffer)), want, sizeof(want), "WLFW mode request");
		memset(&back, 0, sizeof(back));
		check(redwood_qmi_decode(want, sizeof(want), qmi_wlanfw_wlan_mode_req_msg_v01_ei, &back) == (int)sizeof(want) &&
		    back.mode == 7 && back.hw_debug_valid == 1 && back.hw_debug == 0, "WLFW mode request, decoded");
	}
	/* a piece of the board file: an array of bytes with a 2-byte count */
	{
		static const uint8_t want[] = {
			0x00, 0x02, 0x01, 0x25, 0x00, 0x2b, 0x00,
			0x01, 0x01, 0x00, 0x01,                                 /* valid */
			0x10, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00,               /* file_id */
			0x11, 0x04, 0x00, 0x05, 0x00, 0x00, 0x00,               /* total_size */
			0x12, 0x04, 0x00, 0x02, 0x00, 0x00, 0x00,               /* seg_id */
			0x13, 0x07, 0x00, 0x05, 0x00, 0xde, 0xad, 0xbe, 0xef, 0x01,     /* data */
			0x14, 0x01, 0x00, 0x01,                                 /* end */
			0x15, 0x01, 0x00, 0x01,                                 /* bdf_type: ELF */
		};
		static struct qmi_wlanfw_bdf_download_req_msg_v01 req, back;

		memset(&req, 0, sizeof(req));
		req.valid = 1;
		req.file_id_valid = 1;
		req.file_id = 0;
		req.total_size_valid = 1;
		req.total_size = 5;
		req.seg_id_valid = 1;
		req.seg_id = 2;
		req.data_valid = 1;
		req.data_len = 5;
		memcpy(req.data, "\xde\xad\xbe\xef\x01", 5);
		req.end_valid = 1;
		req.end = 1;
		req.bdf_type_valid = 1;
		req.bdf_type = 1;
		check_bytes(buffer, redwood_qmi_encode(QMI_REQUEST, 0x25, 0x0102, qmi_wlanfw_bdf_download_req_msg_v01_ei, &req, buffer,
		    sizeof(buffer)), want, sizeof(want), "WLFW board data request");
		memset(&back, 0, sizeof(back));
		check(redwood_qmi_decode(want, sizeof(want), qmi_wlanfw_bdf_download_req_msg_v01_ei, &back) == (int)sizeof(want) &&
		    back.data_valid == 1 && back.data_len == 5 && memcmp(back.data, "\xde\xad\xbe\xef\x01", 5) == 0 && back.seg_id == 2 &&
		    back.end == 1 && back.bdf_type == 1, "WLFW board data request, decoded");
	}
	/* what the firmware says of itself: structures, a string inside one, a string at the top level */
	{
		static const uint8_t reply[] = {
			0x02, 0x03, 0x00, 0x24, 0x00, 0x61, 0x00,
			0x02, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00,               /* result */
			0x10, 0x08, 0x00, 0x18, 0x00, 0x00, 0x00, 0x0b, 0x00, 0x00, 0x00,       /* chip_info: id 0x18, family 0x0b */
			0x11, 0x04, 0x00, 0xff, 0x00, 0x00, 0x00,               /* board_info: 0xff */
			0x12, 0x04, 0x00, 0x20, 0x03, 0x02, 0x40,               /* soc_info */
			0x13, 0x15, 0x00, 0x21, 0x43, 0x65, 0x87, 0x10,         /* fw_version_info: a number, then a string of 16 */
			'2', '0', '2', '3', '-', '0', '4', '-', '1', '5', ' ', '0', '1', ':', '2', '7',
			0x14, 0x22, 0x00,                                       /* fw_build_id: 34 characters, no length before them */
			'W', 'L', 'A', 'N', '.', 'M', 'S', 'L', '.', '1', '.', '0', '.', '1', '-', '0', '1', '2', '7', '7', '-',
			'Q', 'C', 'A', 'M', 'S', 'L', 'S', 'W', 'C', 'B', 'T', '-', '1',
			0x15, 0x01, 0x00, 0x02,                                 /* num_macs */
		};
		static struct qmi_wlanfw_cap_resp_msg_v01 cap;

		memset(&cap, 0, sizeof(cap));
		check(redwood_qmi_decode(reply, sizeof(reply), qmi_wlanfw_cap_resp_msg_v01_ei, &cap) == (int)sizeof(reply),
		    "WLFW capabilities reply is not decoded");
		check(cap.resp.result == 0 && cap.chip_info_valid == 1 && cap.chip_info.chip_id == 0x18 && cap.chip_info.chip_family == 0x0b &&
		    cap.board_info_valid == 1 && cap.board_info.board_id == 0xff && cap.soc_info_valid == 1 && cap.soc_info.soc_id == 0x40020320 &&
		    cap.fw_version_info_valid == 1 && cap.fw_version_info.fw_version == 0x87654321 &&
		    strcmp(cap.fw_version_info.fw_build_timestamp, "2023-04-15 01:27") == 0 && cap.fw_build_id_valid == 1 &&
		    strcmp(cap.fw_build_id, "WLAN.MSL.1.0.1-01277-QCAMSLSWCBT-1") == 0 && cap.num_macs_valid == 1 && cap.num_macs == 2 &&
		    cap.voltage_mv_valid == 0, "WLFW capabilities reply: the fields are not what the bytes say");
		check_bytes(buffer, redwood_qmi_encode(QMI_RESPONSE, 0x24, 3, qmi_wlanfw_cap_resp_msg_v01_ei, &cap, buffer, sizeof(buffer)),
		    reply, sizeof(reply), "WLFW capabilities reply, encoded again");
	}
	/* "give me memory": an array of structures, each with an array of structures in it */
	{
		static const uint8_t indication[] = {
			0x04, 0x00, 0x00, 0x35, 0x00, 0x23, 0x00,
			0x01, 0x20, 0x00, 0x02,                                 /* two segments */
			0x00, 0x00, 0x40, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,   /*   4 MB of type 1, one configuration: */
			0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,   /*     offset 0 (8 bytes), size 0, secure 0 */
			0x00, 0x00, 0x00, 0x00,
			0x00, 0x00, 0x08, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00,   /*   512 KB of type 4, no configuration */
		};
		static struct qmi_wlanfw_request_mem_ind_msg_v01 ind;

		memset(&ind, 0, sizeof(ind));
		check(redwood_qmi_decode(indication, sizeof(indication), qmi_wlanfw_request_mem_ind_msg_v01_ei, &ind) == (int)sizeof(indication),
		    "WLFW memory request is not decoded");
		check(ind.mem_seg_len == 2 && ind.mem_seg[0].size == 0x400000 && ind.mem_seg[0].type == 1 && ind.mem_seg[0].mem_cfg_len == 1 &&
		    ind.mem_seg[1].size == 0x80000 && ind.mem_seg[1].type == 4 && ind.mem_seg[1].mem_cfg_len == 0,
		    "WLFW memory request: the fields are not what the bytes say");
	}
}


#endif /* !NO_ATH11K */
