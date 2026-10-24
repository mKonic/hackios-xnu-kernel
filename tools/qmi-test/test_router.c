/*
 * The helpers over a router made up here: two ports, transactions, indications, a closing peer.
 * See test_qmi.h for what is shared; run.sh builds the pieces into one program.
 */

#include "test_qmi.h"

/* ---- 3. the helpers, over a router made up here ------------------------------------------------------ */

#define SERVER_NODE     7
#define SERVER_PORT     0x4001

struct redwood_qrtr_port {
	redwood_qrtr_receive_t receive;
	redwood_qrtr_server_t server;
	void *context;
	bool open;
	unsigned lookups, sent, closed;
	uint32_t looked_service, looked_version, looked_instance;
	uint32_t last_node, last_port;
	uint8_t last[8192 + 16];
	size_t last_length;
	int send_error;                 /* what send answers */
	/* what the other side does with a datagram: called from send (as a router that delivers at once would) */
	void (*peer)(struct redwood_qrtr_port *port, const uint8_t *data, size_t length);
};
static struct redwood_qrtr_port router;

static struct redwood_qrtr_port *
router_open(redwood_qrtr_receive_t receive, redwood_qrtr_server_t server, void *context)
{
	memset(&router, 0, sizeof(router));
	router.receive = receive;
	router.server = server;
	router.context = context;
	router.open = true;
	return &router;
}

static int
router_send(struct redwood_qrtr_port *port, uint32_t node, uint32_t to_port, const void *data, size_t length)
{
	if (port->send_error) {
		return port->send_error;
	}
	port->sent++;
	port->last_node = node;
	port->last_port = to_port;
	port->last_length = length;
	memcpy(port->last, data, length < sizeof(port->last) ? length : sizeof(port->last));
	if (port->peer != NULL) {
		port->peer(port, data, length);
	}
	return 0;
}

static int
router_lookup(struct redwood_qrtr_port *port, uint32_t service, uint32_t version, uint32_t instance)
{
	port->lookups++;
	port->looked_service = service;
	port->looked_version = version;
	port->looked_instance = instance;
	return 0;
}

static void
router_close(struct redwood_qrtr_port *port)
{
	port->open = false;
	port->closed++;
}

static const struct redwood_qrtr_ops router_ops = { router_open, router_send, router_lookup, router_close };

/* A datagram from the server to the handle, as the router's thread would bring it. */
static void
deliver(const uint8_t *data, size_t length)
{
	router.receive(router.context, SERVER_NODE, SERVER_PORT, data, length);
}

/* ... a while later, from another thread */
struct later {
	uint8_t data[8192];
	size_t length;
	unsigned ms;
};

static void *
later_thread(void *argument)
{
	struct later *l = argument;

	usleep(l->ms * 1000);
	deliver(l->data, l->length);
	free(l);
	return NULL;
}

static pthread_t
deliver_later(const uint8_t *data, size_t length, unsigned ms)
{
	struct later *l = malloc(sizeof(*l));
	pthread_t thread;

	memcpy(l->data, data, length);
	l->length = length;
	l->ms = ms;
	pthread_create(&thread, NULL, later_thread, l);
	return thread;
}

/* A reply of 7 bytes of result behind a header that answers the request in `request`. */
static size_t
plain_reply(uint8_t *reply, const uint8_t *request, uint16_t result, uint16_t error)
{
	static const uint8_t body[] = { 0x02, 0x04, 0x00 };

	reply[0] = QMI_RESPONSE;
	memcpy(reply + 1, request + 1, 4);      /* the transaction and the message id */
	reply[5] = 7;
	reply[6] = 0;
	memcpy(reply + 7, body, 3);
	reply[10] = (uint8_t)result;
	reply[11] = (uint8_t)(result >> 8);
	reply[12] = (uint8_t)error;
	reply[13] = (uint8_t)(error >> 8);
	return 14;
}

/* a message of our own for this part: a request with one number, a reply with result and one number */
struct ask {
	uint32_t number;
};
struct answer {
	struct qmi_response_type_v01 resp;
	uint8_t twice_valid;
	uint32_t twice;
};
static const struct qmi_elem_info ask_ei[] = {
	{ QMI_UNSIGNED_4_BYTE, 1, sizeof(uint32_t), NO_ARRAY, 0x01, offsetof(struct ask, number), NULL },
	{ QMI_EOTI, 0, 0, NO_ARRAY, 0, 0, NULL },
};
static const struct qmi_elem_info answer_ei[] = {
	{ QMI_STRUCT, 1, sizeof(struct qmi_response_type_v01), NO_ARRAY, 0x02, offsetof(struct answer, resp), qmi_response_type_v01_ei },
	{ QMI_OPT_FLAG, 1, sizeof(uint8_t), NO_ARRAY, 0x10, offsetof(struct answer, twice_valid), NULL },
	{ QMI_UNSIGNED_4_BYTE, 1, sizeof(uint32_t), NO_ARRAY, 0x10, offsetof(struct answer, twice), NULL },
	{ QMI_EOTI, 0, 0, NO_ARRAY, 0, 0, NULL },
};
struct news {
	uint8_t what_valid;
	uint32_t what;
};
static const struct qmi_elem_info news_ei[] = {
	{ QMI_OPT_FLAG, 1, sizeof(uint8_t), NO_ARRAY, 0x10, offsetof(struct news, what_valid), NULL },
	{ QMI_UNSIGNED_4_BYTE, 1, sizeof(uint32_t), NO_ARRAY, 0x10, offsetof(struct news, what), NULL },
	{ QMI_EOTI, 0, 0, NO_ARRAY, 0, 0, NULL },
};
#define MESSAGE_ASK     0x0077
#define MESSAGE_NEWS    0x0078

static struct {
	unsigned arrived, left, news;
	struct qmi_service last;
	struct qmi_service *last_pointer;
	int answer;                     /* what new_server says */
	struct news last_news;
	struct sockaddr_qrtr news_from;
	uint16_t news_transaction;
	bool news_sends;                /* the handler sends a request of its own, as ath11k's handlers may */
} seen;

static int
on_new_server(struct qmi_handle *qmi, struct qmi_service *service)
{
	struct sockaddr_qrtr sq = { .sq_family = AF_QIPCRTR, .sq_node = service->node, .sq_port = service->port };

	seen.arrived++;
	seen.last = *service;
	seen.last_pointer = service;
	if (seen.answer < 0) {
		return seen.answer;
	}
	/* what ath11k does here, word for word but for the type its Linux has for an address */
	return kernel_connect(qmi->sock, (void *)&sq, sizeof(sq), 0);
}

static void
on_del_server(struct qmi_handle *qmi, struct qmi_service *service)
{
	(void)qmi;
	seen.left++;
	check(service == seen.last_pointer, "del_server is given another service than new_server was");
}

static void
on_news(struct qmi_handle *qmi, struct sockaddr_qrtr *sq, struct qmi_txn *txn, const void *decoded)
{
	seen.news++;
	seen.last_news = *(const struct news *)decoded;
	seen.news_from = *sq;
	seen.news_transaction = txn->id;
	if (seen.news_sends) {
		struct ask ask = { 5 };
		struct qmi_txn mine;

		check(qmi_txn_init(qmi, &mine, NULL, NULL) >= 0 && qmi_send_request(qmi, NULL, &mine, MESSAGE_ASK, 16, ask_ei, &ask) == 0,
		    "a handler cannot send");
		qmi_txn_cancel(&mine);
	}
}

static const struct qmi_ops handle_ops = { .new_server = on_new_server, .del_server = on_del_server };
static const struct qmi_msg_handler handle_handlers[] = {
	{ .type = QMI_INDICATION, .msg_id = MESSAGE_NEWS, .ei = news_ei, .decoded_size = sizeof(struct news), .fn = on_news },
	{ 0 },
};

/* The peer of the simplest kind: answers "ask" with twice the number, at once. */
static void
doubling_peer(struct redwood_qrtr_port *port, const uint8_t *data, size_t length)
{
	uint8_t reply[32];
	size_t n;

	(void)port;
	if (length != 14 || data[0] != QMI_REQUEST || (data[3] | data[4] << 8) != MESSAGE_ASK) {
		return;
	}
	n = plain_reply(reply, data, 0, 0);
	reply[5] = 14;
	reply[n++] = 0x10;
	reply[n++] = 4;
	reply[n++] = 0;
	{
		uint32_t twice = 2 * (uint32_t)(data[10] | data[11] << 8 | data[12] << 16 | (uint32_t)data[13] << 24);

		memcpy(reply + n, &twice, 4);
	}
	deliver(reply, n + 4);
}

struct asker {
	struct qmi_handle *qmi;
	uint32_t number;
	int result;
	struct answer answer;
	uint16_t id;
};

static void *
asker_thread(void *argument)
{
	struct asker *a = argument;
	struct ask ask = { a->number };
	struct qmi_txn txn;

	memset(&a->answer, 0, sizeof(a->answer));
	a->id = (uint16_t)qmi_txn_init(a->qmi, &txn, answer_ei, &a->answer);
	a->result = qmi_send_request(a->qmi, NULL, &txn, MESSAGE_ASK, 16, ask_ei, &ask);
	if (a->result == 0) {
		a->result = redwood_qmi_txn_wait_ms(&txn, 3000);
	}
	return NULL;
}

void
test_helpers(void)
{
	struct qmi_handle qmi;
	struct qmi_txn txn, other;
	struct ask ask = { 21 };
	struct answer answer;
	uint8_t reply[64];
	uint64_t start;
	size_t n;
	int id, result;

	/* no router: no handle */
	redwood_qmi_set_transport(NULL);
	check(qmi_handle_init(&qmi, 8192, &handle_ops, handle_handlers) == -ENXIO && qmi.sock == NULL, "a handle is made with no router there");

	redwood_qmi_set_transport(&router_ops);
	memset(&seen, 0, sizeof(seen));
	check(qmi_handle_init(&qmi, 8192, &handle_ops, handle_handlers) == 0 && qmi.sock != NULL && router.open, "no handle with a router there");

	/* a request before anybody is connected to */
	id = qmi_txn_init(&qmi, &txn, answer_ei, &answer);
	check(id >= 0 && txn.id == id, "no transaction (%d)", id);
	check(qmi_send_request(&qmi, NULL, &txn, MESSAGE_ASK, 16, ask_ei, &ask) == -ENOTCONN && router.sent == 0, "a request goes out with nowhere to go");
	qmi_txn_cancel(&txn);

	/* looking the service up; a server appears */
	check(qmi_add_lookup(&qmi, 0x45, 1, 3) == 0 && router.lookups == 1 && router.looked_service == 0x45 && router.looked_version == 1 &&
	    router.looked_instance == 3, "the lookup does not reach the router");
	router.server(router.context, true, 0x45, 1, 3, SERVER_NODE, SERVER_PORT);
	check(seen.arrived == 1 && seen.last.service == 0x45 && seen.last.version == 1 && seen.last.instance == 3 && seen.last.node == SERVER_NODE &&
	    seen.last.port == SERVER_PORT, "new_server is not told of the server");
	router.server(router.context, true, 0x45, 1, 3, SERVER_NODE, SERVER_PORT);
	check(seen.arrived == 1, "the same server arrives twice");

	/* a request and its reply, delivered at once (inside send) */
	router.peer = doubling_peer;
	memset(&answer, 0, sizeof(answer));
	id = qmi_txn_init(&qmi, &txn, answer_ei, &answer);
	check(qmi_send_request(&qmi, NULL, &txn, MESSAGE_ASK, 16, ask_ei, &ask) == 0, "the request is not sent");
	{
		const uint8_t want[] = { 0x00, (uint8_t)id, (uint8_t)(id >> 8), 0x77, 0x00, 0x07, 0x00, 0x01, 0x04, 0x00, 21, 0, 0, 0 };

		check_bytes(router.last, (int)router.last_length, want, sizeof(want), "the request as the router got it");
		check(router.last_node == SERVER_NODE && router.last_port == SERVER_PORT, "the request goes to %u:%u", router.last_node, router.last_port);
	}
	result = redwood_qmi_txn_wait_ms(&txn, 1000);
	check(result == 21 && answer.resp.result == 0 && answer.twice_valid == 1 && answer.twice == 42, "the reply does not arrive (%d, %u)", result,
	    answer.twice);

	/* ... and from the router's own thread, 50 ms later, while the caller waits */
	router.peer = NULL;
	memset(&answer, 0, sizeof(answer));
	id = qmi_txn_init(&qmi, &txn, answer_ei, &answer);
	check(qmi_send_request(&qmi, NULL, &txn, MESSAGE_ASK, 16, ask_ei, &ask) == 0, "the request is not sent");
	n = plain_reply(reply, router.last, 1, QMI_ERR_INTERNAL_V01);
	start = now_ms();
	{
		pthread_t thread = deliver_later(reply, n, 50);

		/* in the caller's jiffies: HZ is 250 here, so 250 of them are a second */
		result = qmi_txn_wait(&txn, 250);
		pthread_join(thread, NULL);
	}
	check(result == 14 && answer.resp.result == 1 && answer.resp.error == QMI_ERR_INTERNAL_V01 && answer.twice_valid == 0 &&
	    now_ms() - start >= 45 && now_ms() - start < 900, "the reply from another thread (%d after %llu ms)", result,
	    (unsigned long long)(now_ms() - start));

	/* no reply: the wait ends when it says it will; the reply that comes after is nobody's */
	id = qmi_txn_init(&qmi, &txn, answer_ei, &answer);
	check(qmi_send_request(&qmi, NULL, &txn, MESSAGE_ASK, 16, ask_ei, &ask) == 0, "the request is not sent");
	n = plain_reply(reply, router.last, 0, 0);
	start = now_ms();
	result = qmi_txn_wait(&txn, 30);                /* 30 jiffies of 4 ms */
	check(result == -ETIMEDOUT && now_ms() - start >= 115 && now_ms() - start < 600, "the wait for nothing: %d after %llu ms", result,
	    (unsigned long long)(now_ms() - start));
	memset(&answer, 0x55, sizeof(answer));
	deliver(reply, n);
	check(answer.resp.result == 0x5555 && seen.news == 0, "a reply after its time is still taken");

	/* a transaction given up; a reply with another number; a reply that cannot be decoded */
	id = qmi_txn_init(&qmi, &txn, answer_ei, &answer);
	check(qmi_send_request(&qmi, NULL, &txn, MESSAGE_ASK, 16, ask_ei, &ask) == 0, "the request is not sent");
	n = plain_reply(reply, router.last, 0, 0);
	qmi_txn_cancel(&txn);
	deliver(reply, n);
	check(answer.resp.result == 0x5555, "a reply to a transaction given up is taken");

	memset(&answer, 0, sizeof(answer));
	id = qmi_txn_init(&qmi, &txn, answer_ei, &answer);
	check(qmi_send_request(&qmi, NULL, &txn, MESSAGE_ASK, 16, ask_ei, &ask) == 0, "the request is not sent");
	n = plain_reply(reply, router.last, 0, 0);
	reply[1] ^= 0x40;
	deliver(reply, n);                              /* not ours */
	reply[1] ^= 0x40;
	reply[8] = 9;                                   /* the TLV says more than the message has */
	deliver(reply, n);
	result = redwood_qmi_txn_wait_ms(&txn, 200);
	check(result == -EINVAL, "a reply that cannot be decoded gives %d", result);

	/* three at once have three numbers; two threads ask, the replies come in the other order */
	{
		struct qmi_txn t1, t2, t3;
		int a = qmi_txn_init(&qmi, &t1, NULL, NULL), b = qmi_txn_init(&qmi, &t2, NULL, NULL), c = qmi_txn_init(&qmi, &t3, NULL, NULL);
		struct asker one = { .qmi = &qmi, .number = 100 }, two = { .qmi = &qmi, .number = 200 };
		pthread_t first, second;
		uint8_t r1[32], r2[32];
		uint32_t value;

		check(a >= 0 && b >= 0 && c >= 0 && a != b && b != c && a != c, "three transactions: %d %d %d", a, b, c);
		qmi_txn_cancel(&t1);
		qmi_txn_cancel(&t2);
		qmi_txn_cancel(&t3);

		pthread_create(&first, NULL, asker_thread, &one);
		usleep(30000);
		memcpy(r1, router.last, 14);
		pthread_create(&second, NULL, asker_thread, &two);
		usleep(30000);
		memcpy(r2, router.last, 14);
		check(memcmp(r1 + 1, r2 + 1, 2) != 0, "two requests in the air have one number");
		/* the second one's reply first; each reply says the number that was asked */
		for (int k = 1; k >= 0; k--) {
			const uint8_t *request = k ? r2 : r1;

			n = plain_reply(reply, request, 0, 0);
			reply[5] = 14;
			memcpy(reply + n, "\x10\x04\x00", 3);
			memcpy(&value, request + 10, 4);
			memcpy(reply + n + 3, &value, 4);
			deliver(reply, n + 7);
		}
		pthread_join(first, NULL);
		pthread_join(second, NULL);
		check(one.result == 21 && two.result == 21 && one.answer.twice == 100 && two.answer.twice == 200, "crossed replies: %d %u, %d %u",
		    one.result, one.answer.twice, two.result, two.answer.twice);
	}

	/* an indication: to its handler, decoded; from the handler a request may be sent */
	{
		static const uint8_t news[] = { 0x04, 0x33, 0x00, 0x78, 0x00, 0x07, 0x00, 0x10, 0x04, 0x00, 0xef, 0xbe, 0xad, 0xde };
		static const uint8_t nobody[] = { 0x04, 0x00, 0x00, 0x99, 0x00, 0x00, 0x00 };
		static const uint8_t request_to_us[] = { 0x00, 0x00, 0x00, 0x78, 0x00, 0x00, 0x00 };
		static const uint8_t bad_news[] = { 0x04, 0x00, 0x00, 0x78, 0x00, 0x04, 0x00, 0x10, 0x04, 0x00, 0xef };
		static uint8_t huge[8192 + 7 + 1];
		unsigned sent = router.sent;

		seen.news_sends = true;
		deliver(news, sizeof(news));
		check(seen.news == 1 && seen.last_news.what_valid == 1 && seen.last_news.what == 0xdeadbeef && seen.news_from.sq_family == AF_QIPCRTR &&
		    seen.news_from.sq_node == SERVER_NODE && seen.news_from.sq_port == SERVER_PORT && seen.news_transaction == 0x33,
		    "the indication does not reach its handler as sent");
		check(router.sent == sent + 1, "the handler's own request did not go out");
		seen.news_sends = false;
		deliver(nobody, sizeof(nobody));
		deliver(request_to_us, sizeof(request_to_us));          /* the same id, but a request: no handler is for that */
		deliver(bad_news, sizeof(bad_news));
		deliver(news, 5);                                       /* shorter than a header */
		memcpy(huge, news, sizeof(news));
		deliver(huge, sizeof(huge));                            /* more than the handle said it takes */
		check(seen.news == 1, "a message that is no indication we know reached the handler (%u)", seen.news);
	}

	/* to another address; the router refusing; the service going away */
	{
		struct sockaddr_qrtr elsewhere = { .sq_family = AF_QIPCRTR, .sq_node = 9, .sq_port = 99 };

		qmi_txn_init(&qmi, &txn, NULL, NULL);
		check(qmi_send_request(&qmi, &elsewhere, &txn, MESSAGE_ASK, 16, ask_ei, &ask) == 0 && router.last_node == 9 && router.last_port == 99,
		    "a request to a given address goes to %u:%u", router.last_node, router.last_port);
		router.send_error = EHOSTUNREACH;
		check(qmi_send_request(&qmi, NULL, &txn, MESSAGE_ASK, 16, ask_ei, &ask) == -EHOSTUNREACH, "the router's refusal is not passed on");
		router.send_error = 0;
		check(qmi_send_request(&qmi, NULL, &txn, MESSAGE_ASK, 2, ask_ei, &ask) == -EMSGSIZE, "a request longer than its caller allows goes out");
		qmi_txn_cancel(&txn);
		qmi_txn_init(&qmi, &other, NULL, NULL);
		qmi_txn_cancel(&other);
	}
	router.server(router.context, false, 0x45, 1, 3, SERVER_NODE, SERVER_PORT);
	check(seen.left == 1, "del_server is not told (%u)", seen.left);
	router.server(router.context, false, 0x45, 1, 3, SERVER_NODE, SERVER_PORT);
	check(seen.left == 1, "the same server leaves twice");
	/* a server new_server does not want is forgotten: its leaving is not told */
	seen.answer = -EIO;
	router.server(router.context, true, 0x45, 1, 3, SERVER_NODE, SERVER_PORT + 1);
	router.server(router.context, false, 0x45, 1, 3, SERVER_NODE, SERVER_PORT + 1);
	check(seen.arrived == 2 && seen.left == 1, "a refused server: arrived %u, left %u", seen.arrived, seen.left);
	seen.answer = 0;
	router.server(router.context, true, 0x45, 1, 3, SERVER_NODE, SERVER_PORT);      /* (one left behind for release to free) */

	qmi_handle_release(&qmi);
	check(router.closed == 1 && !router.open && qmi.sock == NULL, "release does not close the port");
	redwood_qmi_set_transport(NULL);
}
