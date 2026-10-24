/*
 * A whole WCN6750 exchange with ath11k up to firmware ready, both sides played.
 * See test_qmi.h for what is shared; run.sh builds the pieces into one program.
 */

#include "test_qmi.h"

#ifndef NO_ATH11K

/* ---- ... and the whole exchange of a WCN6750 with ath11k, both played here -------------------------- */

/*
 * The firmware's side: answers each request as the real one's replies are described (and checks
 * what it is asked), and sends the indications in between from its own thread. The host's side
 * below does what ath11k's qmi.c does for this chip, in its order, with its message ids, its
 * structures and its largest lengths.
 */
static struct {
	unsigned char file[20000];      /* the board file the host sends */
	unsigned char got[20000];
	size_t got_length;
	unsigned segments, requests[0x50];
	bool complete, bad;
	uint32_t mode[4];
	unsigned modes;
	pthread_t threads[8];
	unsigned thread_count;
} firmware;

static void
firmware_indicates(uint16_t message, unsigned ms)
{
	uint8_t indication[7] = { QMI_INDICATION, 0, 0, (uint8_t)message, (uint8_t)(message >> 8), 0, 0 };

	firmware.threads[firmware.thread_count++] = deliver_later(indication, sizeof(indication), ms);
}

static void
firmware_peer(struct redwood_qrtr_port *port, const uint8_t *data, size_t length)
{
	static uint8_t reply[512];
	uint16_t message = (uint16_t)(data[3] | data[4] << 8);
	int n;

	(void)port;
	if (data[0] != QMI_REQUEST || message >= 0x50) {
		firmware.bad = true;
		return;
	}
	firmware.requests[message]++;
	n = (int)plain_reply(reply, data, 0, 0);
	switch (message) {
	case 0x20: {                    /* indication register */
		static struct qmi_wlanfw_ind_register_req_msg_v01 req;
		struct qmi_wlanfw_ind_register_resp_msg_v01 resp = { .fw_status_valid = 1, .fw_status = 0 };

		memset(&req, 0, sizeof(req));
		firmware.bad |= redwood_qmi_decode(data, length, qmi_wlanfw_ind_register_req_msg_v01_ei, &req) < 0 || !req.fw_ready_enable ||
		    !req.fw_init_done_enable || !req.cal_done_enable || req.request_mem_enable_valid;
		n = redwood_qmi_encode(QMI_RESPONSE, message, (uint16_t)(data[1] | data[2] << 8), qmi_wlanfw_ind_register_resp_msg_v01_ei, &resp,
		    reply, sizeof(reply));
		break;
	}
	case 0x34: {                    /* host capabilities */
		static struct qmi_wlanfw_host_cap_req_msg_v01 req;

		memset(&req, 0, sizeof(req));
		firmware.bad |= redwood_qmi_decode(data, length, qmi_wlanfw_host_cap_req_msg_v01_ei, &req) < 0 || !req.num_clients_valid ||
		    req.num_clients != 1 || !req.mem_cfg_mode_valid || req.m3_support != 0;
		break;
	}
	case 0x24: {                    /* capabilities */
		static struct qmi_wlanfw_cap_resp_msg_v01 resp;

		memset(&resp, 0, sizeof(resp));
		resp.chip_info_valid = 1;
		resp.chip_info.chip_id = 0x18;
		resp.chip_info.chip_family = 0x0b;
		resp.board_info_valid = 1;
		resp.board_info.board_id = 0xff;
		resp.soc_info_valid = 1;
		resp.soc_info.soc_id = 0x40020320;
		resp.fw_version_info_valid = 1;
		resp.fw_version_info.fw_version = 0x10100504;
		strcpy(resp.fw_version_info.fw_build_timestamp, "2023-04-15 01:27");
		resp.fw_build_id_valid = 1;
		strcpy(resp.fw_build_id, "WLAN.MSL.1.0.1-01277-QCAMSLSWCBT-1");
		firmware.bad |= length != QMI_HEADER_SIZE;
		n = redwood_qmi_encode(QMI_RESPONSE, message, (uint16_t)(data[1] | data[2] << 8), qmi_wlanfw_cap_resp_msg_v01_ei, &resp, reply,
		    sizeof(reply));
		break;
	}
	case 0x4c: {                    /* device info: where the registers are */
		struct qmi_wlanfw_device_info_resp_msg_v01 resp = { .bar_addr = 0x17a00000, .bar_size = 0x200000, .bar_addr_valid = 1,
			                                            .bar_size_valid = 1 };

		n = redwood_qmi_encode(QMI_RESPONSE, message, (uint16_t)(data[1] | data[2] << 8), qmi_wlfw_device_info_resp_msg_v01_ei, &resp,
		    reply, sizeof(reply));
		break;
	}
	case 0x25: {                    /* a piece of a file */
		static struct qmi_wlanfw_bdf_download_req_msg_v01 req;

		memset(&req, 0, sizeof(req));
		if (redwood_qmi_decode(data, length, qmi_wlanfw_bdf_download_req_msg_v01_ei, &req) < 0 || !req.valid || !req.data_valid ||
		    req.seg_id != firmware.segments || req.total_size != sizeof(firmware.file) ||
		    firmware.got_length + req.data_len > sizeof(firmware.got)) {
			firmware.bad = true;
			break;
		}
		memcpy(firmware.got + firmware.got_length, req.data, req.data_len);
		firmware.got_length += req.data_len;
		firmware.segments++;
		if (req.end) {
			firmware.complete = true;
			firmware_indicates(QMI_WLFW_FW_INIT_DONE_IND_V01, 20);
		}
		break;
	}
	case 0x22: {                    /* mode */
		struct qmi_wlanfw_wlan_mode_req_msg_v01 req = { 0 };

		firmware.bad |= redwood_qmi_decode(data, length, qmi_wlanfw_wlan_mode_req_msg_v01_ei, &req) < 0 || firmware.modes >= 4;
		firmware.mode[firmware.modes++ & 3] = req.mode;
		if (req.mode == 7) {            /* cold boot calibration: done after a while, then ready */
			firmware_indicates(QMI_WLFW_COLD_BOOT_CAL_DONE_IND_V01, 30);
			firmware_indicates(QMI_WLFW_FW_READY_IND_V01, 60);
		}
		break;
	}
	case 0x23: {                    /* configuration: the pipes */
		static struct qmi_wlanfw_wlan_cfg_req_msg_v01 req;

		memset(&req, 0, sizeof(req));
		firmware.bad |= redwood_qmi_decode(data, length, qmi_wlanfw_wlan_cfg_req_msg_v01_ei, &req) < 0 || !req.tgt_cfg_valid ||
		    req.tgt_cfg_len != 9 || req.tgt_cfg[8].pipe_num != 8 || !req.svc_cfg_valid || req.svc_cfg_len != 20 ||
		    strcmp(req.host_version, "WIN") != 0;
		break;
	}
	default:
		firmware.bad = true;
		break;
	}
	if (n > 0) {
		deliver(reply, (size_t)n);
	}
}

static struct {
	pthread_mutex_t lock;
	pthread_cond_t changed;
	bool server, init_done, cal_done, ready;
	struct qmi_service service;
} host = { PTHREAD_MUTEX_INITIALIZER, PTHREAD_COND_INITIALIZER, false, false, false, false, { 0 } };

static void
host_event(bool *what)
{
	pthread_mutex_lock(&host.lock);
	*what = true;
	pthread_cond_broadcast(&host.changed);
	pthread_mutex_unlock(&host.lock);
}

static bool
host_waits(const bool *what, unsigned ms)
{
	struct timespec until;
	bool happened;

	clock_gettime(CLOCK_REALTIME, &until);
	until.tv_sec += ms / 1000;
	until.tv_nsec += (long)(ms % 1000) * 1000000;
	if (until.tv_nsec >= 1000000000) {
		until.tv_sec++;
		until.tv_nsec -= 1000000000;
	}
	pthread_mutex_lock(&host.lock);
	while (!*what && pthread_cond_timedwait(&host.changed, &host.lock, &until) == 0) {
	}
	happened = *what;
	pthread_mutex_unlock(&host.lock);
	return happened;
}

static int
host_new_server(struct qmi_handle *qmi, struct qmi_service *service)
{
	struct sockaddr_qrtr sq = { .sq_family = AF_QIPCRTR, .sq_node = service->node, .sq_port = service->port };
	int error = kernel_connect(qmi->sock, (void *)&sq, sizeof(sq), 0);

	host.service = *service;
	host_event(&host.server);
	return error;
}

static void
host_init_done(struct qmi_handle *qmi, struct sockaddr_qrtr *sq, struct qmi_txn *txn, const void *decoded)
{
	(void)qmi; (void)sq; (void)txn; (void)decoded;
	host_event(&host.init_done);
}

static void
host_cal_done(struct qmi_handle *qmi, struct sockaddr_qrtr *sq, struct qmi_txn *txn, const void *decoded)
{
	(void)qmi; (void)sq; (void)txn; (void)decoded;
	host_event(&host.cal_done);
}

static void
host_ready(struct qmi_handle *qmi, struct sockaddr_qrtr *sq, struct qmi_txn *txn, const void *decoded)
{
	(void)qmi; (void)sq; (void)txn; (void)decoded;
	host_event(&host.ready);
}

static const struct qmi_ops host_ops = { .new_server = host_new_server };
/* (the same list as ath11k's qmi.c has, with its tables and sizes) */
static const struct qmi_msg_handler host_handlers[] = {
	{ .type = QMI_INDICATION, .msg_id = QMI_WLFW_FW_READY_IND_V01, .ei = qmi_wlanfw_fw_ready_ind_msg_v01_ei,
	  .decoded_size = sizeof(struct qmi_wlanfw_fw_ready_ind_msg_v01), .fn = host_ready },
	{ .type = QMI_INDICATION, .msg_id = QMI_WLFW_COLD_BOOT_CAL_DONE_IND_V01, .ei = qmi_wlanfw_cold_boot_cal_done_ind_msg_v01_ei,
	  .decoded_size = sizeof(struct qmi_wlanfw_fw_cold_cal_done_ind_msg_v01), .fn = host_cal_done },
	{ .type = QMI_INDICATION, .msg_id = QMI_WLFW_FW_INIT_DONE_IND_V01, .ei = qmi_wlfw_fw_init_done_ind_msg_v01_ei,
	  .decoded_size = sizeof(struct qmi_wlfw_fw_init_done_ind_msg_v01), .fn = host_init_done },
	{ 0 },
};

/* One exchange the way every one of qmi.c's ten goes. Returns what qmi_txn_wait() gave. */
static int
exchange(struct qmi_handle *qmi, int message, size_t largest, const struct qmi_elem_info *request_ei, const void *request,
    const struct qmi_elem_info *reply_ei, void *reply)
{
	struct qmi_txn txn;
	int ret;

	ret = qmi_txn_init(qmi, &txn, reply_ei, reply);
	if (ret < 0) {
		return ret;
	}
	ret = qmi_send_request(qmi, NULL, &txn, message, largest, request_ei, request);
	if (ret < 0) {
		qmi_txn_cancel(&txn);
		return ret;
	}
	return qmi_txn_wait(&txn, msecs_to_jiffies(ATH11K_QMI_WLANFW_TIMEOUT_MS));
}

void
test_conversation(void)
{
	static struct qmi_wlanfw_bdf_download_req_msg_v01 bdf;
	static struct qmi_wlanfw_wlan_cfg_req_msg_v01 cfg;
	static struct qmi_wlanfw_cap_resp_msg_v01 cap;
	static struct qmi_wlanfw_host_cap_req_msg_v01 host_cap;
	struct qmi_wlanfw_ind_register_req_msg_v01 reg;
	struct qmi_wlanfw_ind_register_resp_msg_v01 reg_resp = { 0 };
	struct qmi_wlanfw_host_cap_resp_msg_v01 host_cap_resp = { 0 };
	struct qmi_wlanfw_cap_req_msg_v01 cap_req = { 0 };
	struct qmi_wlanfw_device_info_req_msg_v01 info_req = { 0 };
	struct qmi_wlanfw_device_info_resp_msg_v01 info = { 0 };
	struct qmi_wlanfw_bdf_download_resp_msg_v01 bdf_resp;
	struct qmi_wlanfw_wlan_mode_req_msg_v01 mode;
	struct qmi_wlanfw_wlan_mode_resp_msg_v01 mode_resp = { 0 };
	struct qmi_wlanfw_wlan_cfg_resp_msg_v01 cfg_resp = { 0 };
	struct qmi_handle qmi;
	size_t left = sizeof(firmware.file), longest = 0;
	const unsigned char *at = firmware.file;
	int ret;

	for (size_t i = 0; i < sizeof(firmware.file); i++) {
		firmware.file[i] = (unsigned char)(rnd() >> 11);
	}
	redwood_qmi_set_transport(&router_ops);
	check(qmi_handle_init(&qmi, ATH11K_QMI_RESP_LEN_MAX, &host_ops, host_handlers) == 0, "conversation: no handle");
	router.peer = firmware_peer;
	check(qmi_add_lookup(&qmi, ATH11K_QMI_WLFW_SERVICE_ID_V01, ATH11K_QMI_WLFW_SERVICE_VERS_V01,
	    ATH11K_QMI_WLFW_SERVICE_INS_ID_V01_WCN6750) == 0 && router.looked_service == 0x45 && router.looked_instance == 3, "conversation: lookup");
	router.server(router.context, true, 0x45, 1, 3, SERVER_NODE, SERVER_PORT);
	check(host_waits(&host.server, 1000), "conversation: the server does not arrive");

	/* indication register: fixed firmware memory, so no memory indications are asked for */
	memset(&reg, 0, sizeof(reg));
	reg.client_id_valid = 1;
	reg.client_id = 0x4b4e454c;
	reg.fw_ready_enable_valid = 1;
	reg.fw_ready_enable = 1;
	reg.cal_done_enable_valid = 1;
	reg.cal_done_enable = 1;
	reg.fw_init_done_enable_valid = 1;
	reg.fw_init_done_enable = 1;
	reg.pin_connect_result_enable_valid = 1;
	reg.pin_connect_result_enable = 1;
	ret = exchange(&qmi, 0x20, QMI_WLANFW_IND_REGISTER_REQ_MSG_V01_MAX_LEN, qmi_wlanfw_ind_register_req_msg_v01_ei, &reg,
	    qmi_wlanfw_ind_register_resp_msg_v01_ei, &reg_resp);
	check(ret >= 0 && reg_resp.resp.result == QMI_RESULT_SUCCESS_V01 && reg_resp.fw_status_valid == 1, "conversation: indication register (%d)", ret);

	memset(&host_cap, 0, sizeof(host_cap));
	host_cap.num_clients_valid = 1;
	host_cap.num_clients = 1;
	host_cap.mem_cfg_mode = 0;
	host_cap.mem_cfg_mode_valid = 1;
	host_cap.bdf_support_valid = 1;
	host_cap.bdf_support = 1;
	host_cap.m3_support_valid = 1;
	host_cap.m3_support = 0;
	host_cap.m3_cache_support_valid = 1;
	host_cap.m3_cache_support = 0;
	host_cap.cal_done_valid = 1;
	host_cap.cal_done = 0;
	ret = exchange(&qmi, 0x34, QMI_WLANFW_HOST_CAP_REQ_MSG_V01_MAX_LEN, qmi_wlanfw_host_cap_req_msg_v01_ei, &host_cap,
	    qmi_wlanfw_host_cap_resp_msg_v01_ei, &host_cap_resp);
	check(ret >= 0 && host_cap_resp.resp.result == 0, "conversation: host capabilities (%d)", ret);

	memset(&cap, 0, sizeof(cap));
	ret = exchange(&qmi, 0x24, QMI_WLANFW_CAP_REQ_MSG_V01_MAX_LEN, qmi_wlanfw_cap_req_msg_v01_ei, &cap_req, qmi_wlanfw_cap_resp_msg_v01_ei, &cap);
	check(ret >= 0 && cap.resp.result == 0 && cap.chip_info.chip_id == 0x18 && cap.board_info.board_id == 0xff &&
	    cap.soc_info.soc_id == 0x40020320 && strcmp(cap.fw_build_id, "WLAN.MSL.1.0.1-01277-QCAMSLSWCBT-1") == 0 &&
	    strcmp(cap.fw_version_info.fw_build_timestamp, "2023-04-15 01:27") == 0, "conversation: capabilities (%d)", ret);

	ret = exchange(&qmi, 0x4c, QMI_WLANFW_DEVICE_INFO_REQ_MSG_V01_MAX_LEN, qmi_wlanfw_device_info_req_msg_v01_ei, &info_req,
	    qmi_wlfw_device_info_resp_msg_v01_ei, &info);
	check(ret >= 0 && info.bar_addr_valid && info.bar_addr == 0x17a00000 && info.bar_size == ATH11K_QMI_DEVICE_BAR_SIZE,
	    "conversation: device info (%d)", ret);

	/* the board file, 6144 bytes at a time, as ath11k_qmi_load_file_target_mem() sends it */
	while (left > 0) {
		memset(&bdf, 0, sizeof(bdf));
		bdf.valid = 1;
		bdf.file_id_valid = 1;
		bdf.file_id = cap.board_info.board_id;
		bdf.total_size_valid = 1;
		bdf.total_size = sizeof(firmware.file);
		bdf.seg_id_valid = 1;
		bdf.seg_id = firmware.segments;
		bdf.data_valid = 1;
		bdf.bdf_type = ATH11K_QMI_BDF_TYPE_ELF;
		bdf.bdf_type_valid = 1;
		bdf.end_valid = 1;
		bdf.data_len = left > QMI_WLANFW_MAX_DATA_SIZE_V01 ? QMI_WLANFW_MAX_DATA_SIZE_V01 : (uint32_t)left;
		bdf.end = left <= QMI_WLANFW_MAX_DATA_SIZE_V01;
		memcpy(bdf.data, at, bdf.data_len);
		memset(&bdf_resp, 0, sizeof(bdf_resp));
		ret = exchange(&qmi, 0x25, QMI_WLANFW_BDF_DOWNLOAD_REQ_MSG_V01_MAX_LEN, qmi_wlanfw_bdf_download_req_msg_v01_ei, &bdf,
		    qmi_wlanfw_bdf_download_resp_msg_v01_ei, &bdf_resp);
		if (ret < 0 || bdf_resp.resp.result != 0) {
			check(false, "conversation: board file, piece %u (%d)", firmware.segments, ret);
			break;
		}
		longest = router.last_length > longest ? router.last_length : longest;
		at += bdf.data_len;
		left -= bdf.data_len;
	}
	check(firmware.complete && firmware.segments == 4 && firmware.got_length == sizeof(firmware.file) &&
	    memcmp(firmware.got, firmware.file, sizeof(firmware.file)) == 0, "conversation: the board file did not arrive whole (%u pieces, %zu bytes)",
	    firmware.segments, firmware.got_length);
	check(longest == 6182 + QMI_HEADER_SIZE, "conversation: the longest datagram was %zu bytes, not the 6189 agreed", longest);

	check(host_waits(&host.init_done, 2000), "conversation: no \"firmware init done\"");
	mode.mode = 7;                          /* ATH11K_FIRMWARE_MODE_COLD_BOOT */
	mode.hw_debug_valid = 1;
	mode.hw_debug = 0;
	ret = exchange(&qmi, 0x22, QMI_WLANFW_WLAN_MODE_REQ_MSG_V01_MAX_LEN, qmi_wlanfw_wlan_mode_req_msg_v01_ei, &mode,
	    qmi_wlanfw_wlan_mode_resp_msg_v01_ei, &mode_resp);
	check(ret >= 0 && mode_resp.resp.result == 0, "conversation: cold boot mode (%d)", ret);
	check(host_waits(&host.cal_done, 2000), "conversation: no \"cold boot calibration done\"");
	check(host_waits(&host.ready, 2000), "conversation: no \"firmware ready\"");

	/* the configuration: nine pipes, twenty services, as wcn6750's tables have them */
	memset(&cfg, 0, sizeof(cfg));
	strcpy(cfg.host_version, ATH11K_HOST_VERSION_STRING);
	cfg.host_version_valid = 1;
	cfg.tgt_cfg_valid = 1;
	cfg.tgt_cfg_len = 9;
	for (uint32_t i = 0; i < cfg.tgt_cfg_len; i++) {
		cfg.tgt_cfg[i].pipe_num = i;
		cfg.tgt_cfg[i].pipe_dir = i & 1;
		cfg.tgt_cfg[i].nentries = 32;
		cfg.tgt_cfg[i].nbytes_max = 2048;
	}
	cfg.svc_cfg_valid = 1;
	cfg.svc_cfg_len = 20;
	for (uint32_t i = 0; i < cfg.svc_cfg_len; i++) {
		cfg.svc_cfg[i].service_id = 0x100 + i;
		cfg.svc_cfg[i].pipe_dir = i & 1;
		cfg.svc_cfg[i].pipe_num = i % 9;
	}
	cfg.shadow_reg_v2_valid = 1;
	cfg.shadow_reg_v2_len = 36;
	ret = exchange(&qmi, 0x23, QMI_WLANFW_WLAN_CFG_REQ_MSG_V01_MAX_LEN, qmi_wlanfw_wlan_cfg_req_msg_v01_ei, &cfg,
	    qmi_wlanfw_wlan_cfg_resp_msg_v01_ei, &cfg_resp);
	check(ret >= 0 && cfg_resp.resp.result == 0, "conversation: configuration (%d)", ret);

	mode.mode = 0;                          /* ATH11K_FIRMWARE_MODE_NORMAL */
	ret = exchange(&qmi, 0x22, QMI_WLANFW_WLAN_MODE_REQ_MSG_V01_MAX_LEN, qmi_wlanfw_wlan_mode_req_msg_v01_ei, &mode,
	    qmi_wlanfw_wlan_mode_resp_msg_v01_ei, &mode_resp);
	check(ret >= 0 && mode_resp.resp.result == 0 && firmware.modes == 2 && firmware.mode[0] == 7 && firmware.mode[1] == 0,
	    "conversation: mission mode (%d)", ret);

	check(!firmware.bad, "conversation: the firmware's side saw something it did not expect");
	check(firmware.requests[0x20] == 1 && firmware.requests[0x34] == 1 && firmware.requests[0x24] == 1 && firmware.requests[0x4c] == 1 &&
	    firmware.requests[0x25] == 4 && firmware.requests[0x22] == 2 && firmware.requests[0x23] == 1, "conversation: the requests were not the expected ones");
	for (unsigned i = 0; i < firmware.thread_count; i++) {
		pthread_join(firmware.threads[i], NULL);
	}
	qmi_handle_release(&qmi);
	redwood_qmi_set_transport(NULL);
}

#endif /* !NO_ATH11K */
