/*
 * A virtio sound device as the sink of /dev/redwood-audio (see sys/redwood_audio.h).
 *
 * Under QEMU's "virt" machine a "virtio-sound-device" plays what iOS plays: through the host's
 * sound system, or into a file ("-audiodev wav"), whichever back end QEMU is given. Like the
 * input devices (redwood_virtio_input.c) it can sit on the machine's virtio-mmio bus (which must
 * be the current one, not "legacy"; boot-arg virtio_sound=1 turns that bus on) or be a
 * virtio-pci device (-device virtio-sound-pci), used unless virtio_sound=0. Any transport of
 * sys/redwood_virtio.h.
 *
 * The device has four queues; two are used. Over the control queue its first output stream is
 * set to the one format of /dev/redwood-audio and started, once and for good: a started stream
 * that is given nothing plays nothing. The samples go out over the transmit queue, a hundredth
 * of a second to a buffer. The device gives a buffer back when it has played it, which is what
 * paces us: what does not fit into the buffers waits in a ring, and what does not fit into the
 * ring is lost.
 *
 * No interrupt is used; while there is sound on its way the queue is looked at 200 times a
 * second, otherwise not at all.
 *
 * The protocol: Virtual I/O Device (VIRTIO) Version 1.2, section 5.14 "Sound Device".
 */
#include <kern/clock.h>
#include <kern/locks.h>
#include <kern/thread_call.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <mach/vm_param.h>
#include <pexpert/pexpert.h>
#include <sys/redwood_audio.h>
#include <sys/redwood_virtio.h>
#include <sys/systm.h>

#define QUEUE_CONTROL           0
#define QUEUE_TX                2
#define QUEUE_SIZE              64
#define CFG_STREAMS             4               /* u32 after the jacks */

#define SND_R_PCM_INFO          0x0100
#define SND_R_PCM_SET_PARAMS    0x0101
#define SND_R_PCM_PREPARE       0x0102
#define SND_R_PCM_START         0x0104
#define SND_S_OK                0x8000
#define SND_D_OUTPUT            0
#define SND_PCM_FMT_S16         5
#define SND_PCM_RATE_48000      7

struct snd_query_info {
	uint32_t code;
	uint32_t start_id, count, size;
};
#define SND_PCM_INFO_SIZE       32              /* hda_fn_nid, features, formats (64 bits), rates (64 bits), ... */
#define SND_PCM_INFO_DIRECTION  24              /* ... direction, channels_min, channels_max (a byte each) */
struct snd_pcm_hdr {
	uint32_t code;
	uint32_t stream_id;
};
struct snd_pcm_set_params {
	uint32_t code;
	uint32_t stream_id;
	uint32_t buffer_bytes, period_bytes;
	uint32_t features;
	uint8_t  channels, format, rate, padding;
};
struct snd_pcm_status {
	uint32_t status;
	uint32_t latency_bytes;
};

#define PERIOD_FRAMES           (REDWOOD_AUDIO_RATE / 100)
#define PERIOD_BYTES            (PERIOD_FRAMES * REDWOOD_AUDIO_FRAME)
#define TX_BUFFERS              (QUEUE_SIZE / 2)        /* two descriptors each: 0.32 s in the device at most */
#define RING_FRAMES             16384                   /* a power of two: 0.34 s more */
#define MAX_STREAMS             8

/* one transmit buffer: what the device reads, then what it writes */
struct tx_buffer {
	uint32_t stream_id;
	int16_t  frames[PERIOD_FRAMES * REDWOOD_AUDIO_CHANNELS];
	struct snd_pcm_status status;
};

/* what the device reads and writes: the two queues' rings in the first page, then the buffers */
#define OFFSET_CONTROL          0x0000                  /* a request, and at +0x800 the answer */
#define OFFSET_TX               0x1000
#define SHARED_SIZE             round_page(OFFSET_TX + TX_BUFFERS * sizeof(struct tx_buffer))

static vio_dev_t *sound_dev;
static vio_queue_t *control, *tx;
static vm_offset_t shared;
static uint64_t shared_phys;
static uint32_t stream;

static LCK_GRP_DECLARE(sound_lck_grp, "redwood-virtio-sound");
static LCK_MTX_DECLARE(sound_lock, &sound_lck_grp);
static int16_t ring[RING_FRAMES * REDWOOD_AUDIO_CHANNELS];
static uint32_t ring_head, ring_tail;           /* in frames; play from head, add at tail */
static uint16_t tx_free[TX_BUFFERS];
static uint32_t tx_free_count;
static thread_call_t poll_call;
static bool poll_armed;

static bool
control_request(const void *request, uint32_t request_size, void *answer, uint32_t answer_size)
{
	uint8_t *out = (uint8_t *)(shared + OFFSET_CONTROL), *in = out + 0x800;
	struct vio_sg sg[2] = {
		{ shared_phys + OFFSET_CONTROL, request_size, false },
		{ shared_phys + OFFSET_CONTROL + 0x800, answer_size, true },
	};

	memcpy(out, request, request_size);
	bzero(in, answer_size);
	if (vio_queue_add(control, sg, 2, out) != 0) {
		return false;
	}
	vio_queue_kick(control);
	for (int tries = 0; vio_queue_get(control, NULL) == NULL; tries++) {
		if (tries == 1000) {
			printf("virtio-sound: no answer to request %#x\n", *(const uint32_t *)request);
			return false;
		}
		delay_for_interval(1, NSEC_PER_MSEC);
	}
	memcpy(answer, in, answer_size);
	if (*(const uint32_t *)answer != SND_S_OK) {
		printf("virtio-sound: request %#x refused: %#x\n", *(const uint32_t *)request, *(const uint32_t *)answer);
		return false;
	}
	return true;
}

static void
sound_pump(void)
{
	void *done;
	bool sent = false;

	/* cookies are buffer number + 1 */
	while ((done = vio_queue_get(tx, NULL)) != NULL) {
		tx_free[tx_free_count++] = (uint16_t)((uintptr_t)done - 1);
	}
	while (tx_free_count != 0 && ring_tail - ring_head >= PERIOD_FRAMES) {
		uint16_t n = tx_free[--tx_free_count];
		struct tx_buffer *buffer = (struct tx_buffer *)(shared + OFFSET_TX) + n;
		uint64_t pa = shared_phys + OFFSET_TX + (uint64_t)n * sizeof(struct tx_buffer);
		struct vio_sg sg[2] = {
			{ pa, offsetof(struct tx_buffer, status), false },
			{ pa + offsetof(struct tx_buffer, status), sizeof(struct snd_pcm_status), true },
		};

		buffer->stream_id = stream;
		for (uint32_t i = 0; i < PERIOD_FRAMES; i++, ring_head++) {
			buffer->frames[2 * i] = ring[2 * (ring_head % RING_FRAMES)];
			buffer->frames[2 * i + 1] = ring[2 * (ring_head % RING_FRAMES) + 1];
		}
		(void)vio_queue_add(tx, sg, 2, (void *)(uintptr_t)(n + 1));
		sent = true;
	}
	if (sent) {
		vio_queue_kick(tx);
	}
}

static void
sound_poll(__unused thread_call_param_t p0, __unused thread_call_param_t p1)
{
	uint64_t deadline;

	lck_mtx_lock(&sound_lock);
	sound_pump();
	/* as long as the device has buffers of ours, or there is a whole one to give it */
	poll_armed = tx_free_count != TX_BUFFERS || ring_tail - ring_head >= PERIOD_FRAMES;
	if (poll_armed) {
		clock_interval_to_deadline(5, NSEC_PER_MSEC, &deadline);
		thread_call_enter_delayed(poll_call, deadline);
	}
	lck_mtx_unlock(&sound_lock);
}

/* The sink: see sys/redwood_audio.h. */
static uint32_t
sound_play(const int16_t *frames, uint32_t count)
{
	uint32_t room, taken;
	uint64_t deadline;

	lck_mtx_lock(&sound_lock);
	room = RING_FRAMES - (ring_tail - ring_head);
	taken = count < room ? count : room;
	for (uint32_t i = 0; i < taken; i++, ring_tail++) {
		ring[2 * (ring_tail % RING_FRAMES)] = frames[2 * i];
		ring[2 * (ring_tail % RING_FRAMES) + 1] = frames[2 * i + 1];
	}
	sound_pump();
	if (!poll_armed) {
		poll_armed = true;
		clock_interval_to_deadline(5, NSEC_PER_MSEC, &deadline);
		thread_call_enter_delayed(poll_call, deadline);
	}
	lck_mtx_unlock(&sound_lock);
	return taken;
}

static bool
sound_attach(vio_dev_t *dev)
{
	uint8_t streams[sizeof(uint32_t) + MAX_STREAMS * SND_PCM_INFO_SIZE];     /* a status word, then the streams */
	struct snd_query_info query = { .code = SND_R_PCM_INFO, .size = SND_PCM_INFO_SIZE };
	struct snd_pcm_set_params params = { .code = SND_R_PCM_SET_PARAMS, .buffer_bytes = TX_BUFFERS * PERIOD_BYTES,
		                             .period_bytes = PERIOD_BYTES, .channels = REDWOOD_AUDIO_CHANNELS,
		                             .format = SND_PCM_FMT_S16, .rate = SND_PCM_RATE_48000 };
	struct snd_pcm_hdr command = { 0 };
	uint32_t status, count;
	int error;

	sound_dev = dev;
	if ((shared = (vm_offset_t)vio_dma_alloc(SHARED_SIZE, &shared_phys)) == 0) {
		return false;
	}
	if ((error = vio_init(dev, 0, NULL)) != 0) {
		printf("virtio-sound: %s: features refused (%d)\n", vio_name(dev), error);
		return false;
	}
	if (vio_queue_setup(dev, QUEUE_CONTROL, QUEUE_SIZE, &control) != 0 ||
	    vio_queue_setup(dev, QUEUE_TX, QUEUE_SIZE, &tx) != 0) {
		printf("virtio-sound: %s: no queues\n", vio_name(dev));
		vio_fail(dev);
		return false;
	}
	for (uint16_t n = 0; n < TX_BUFFERS && n < vio_queue_size(tx) / 2; n++) {
		tx_free[tx_free_count++] = n;
	}
	vio_driver_ok(dev);

	/* the first stream that plays */
	count = vio_cfg_read32(dev, CFG_STREAMS);
	if (count > MAX_STREAMS) {
		count = MAX_STREAMS;
	}
	query.count = count;
	if (count == 0 || !control_request(&query, sizeof(query), streams, sizeof(uint32_t) + count * SND_PCM_INFO_SIZE)) {
		return false;
	}
	for (stream = 0; stream < count; stream++) {
		if (streams[sizeof(uint32_t) + stream * SND_PCM_INFO_SIZE + SND_PCM_INFO_DIRECTION] == SND_D_OUTPUT) {
			break;
		}
	}
	if (stream == count) {
		printf("virtio-sound: %s: no output stream among %u\n", vio_name(dev), count);
		return false;
	}
	params.stream_id = command.stream_id = stream;
	if (!control_request(&params, sizeof(params), &status, sizeof(status))) {
		return false;
	}
	command.code = SND_R_PCM_PREPARE;
	if (!control_request(&command, sizeof(command), &status, sizeof(status))) {
		return false;
	}
	command.code = SND_R_PCM_START;
	return control_request(&command, sizeof(command), &status, sizeof(status));
}

void
redwood_virtio_sound_init(void)
{
	uint32_t enabled = 1;
	vio_dev_t *dev;

	if (PE_parse_boot_argn("virtio_sound", &enabled, sizeof(enabled)) && !enabled) {
		return;
	}
	if ((dev = vio_find(VIRTIO_ID_SOUND, 0)) == NULL) {
		return;
	}
	if (sound_attach(dev)) {
		printf("virtio-sound: device on %s, stream %u\n", vio_name(dev), stream);
		poll_call = thread_call_allocate(sound_poll, NULL);
		redwood_audio_set_sink("the virtio sound device", sound_play);
	} else {
		printf("virtio-sound: %s: not used\n", vio_name(dev));
	}
}
