/* Private FT3680 protocol implementation for redwood_goodix.c. */
/* ---- the FocalTech FT3680 chip --------------------------------------------------------------------- */

/*
 * FTS 5.x SPI framing from sm8350-kernel-OKX drivers/input/touchscreen/focaltech_3680/
 * focaltech_spi.c (verbatim):
 *   TX[0] = register address (writebuf[0] / cmd[0])
 *   TX[1] = FOCALTECH_READ_CTRL (0xA0 = READ_CMD, CRC_EN set) or FOCALTECH_WRITE_CTRL (0x00)
 *   TX[2..3] = data length BE (for read: datalen; for write: writelen - 1)
 *   TX[4..6] = three dummy bytes (SPI_DUMMY_BYTE)
 *   TX[7..7+len-1] = write data (empty on read; chip drives MISO here)
 *   TX[last-1..last] = CRC-16/Kermit of the data bytes, little-endian (only when READ_CMD or
 *                     when a vendor WRITE op needs CRC; vendor WRITE_CMD = 0x00 has no CRC)
 * SPI_HEADER_LENGTH = 4 + SPI_DUMMY_BYTE = 7.
 */
#define FOCALTECH_READ_CTRL         0xa0		/* READ_CMD = 0x80 | DATA_CRC_EN(0x20) */
#define FOCALTECH_WRITE_CTRL        0x00		/* WRITE_CMD, no CRC on TX */
#define FOCALTECH_CHIP_ID_H         0xa3
#define FOCALTECH_CHIP_ID_L         0x9f
#define FOCALTECH_TOUCH_DATA        0x01		/* the touch report lives at this address */
#define FOCALTECH_MAX_TOUCHES       10
#define FOCALTECH_TOUCH_REPORT_LEN  (2 + 6 * FOCALTECH_MAX_TOUCHES)
#define FOCALTECH_MAX_TRANSFER      (7 + FOCALTECH_TOUCH_REPORT_LEN + 2)

static uint8_t focaltech_tx[FOCALTECH_MAX_TRANSFER], focaltech_rx[FOCALTECH_MAX_TRANSFER];

/* CRC-16/Kermit (polynomial 0x8408 reflected, initial 0xffff) -- same as vendor rckit_crc16(). */
static uint16_t
focaltech_crc(const uint8_t *data, uint32_t length)
{
	uint16_t crc = 0xffff;

	for (uint32_t i = 0; i < length; i++) {
		crc ^= data[i];
		for (int bit = 0; bit < 8; bit++) {
			crc = (crc & 1) ? (uint16_t)((crc >> 1) ^ 0x8408) : (uint16_t)(crc >> 1);
		}
	}
	return crc;
}

static bool
focaltech_read(uint8_t address, uint8_t *data, uint32_t length)
{
	const uint32_t data_offset = 7;
	uint32_t transfer_length = data_offset + length + 2;

	if (length == 0 || transfer_length > sizeof(focaltech_tx)) {
		return false;
	}
	for (int attempt = 0; attempt < 3; attempt++) {
		memset(focaltech_tx, 0, transfer_length);
		memset(focaltech_rx, 0, transfer_length);
		focaltech_tx[0] = address;		/* vendor: TX[0] = cmd[0] = reg address */
		focaltech_tx[1] = FOCALTECH_READ_CTRL;	/* vendor: TX[1] = ctrl = 0xA0 */
		focaltech_tx[2] = (uint8_t)(length >> 8);
		focaltech_tx[3] = (uint8_t)length;
		if (!spi_transfer(focaltech_tx, focaltech_rx, transfer_length)) {
			sleep_us(150);
			continue;
		}
		/* vendor: bit 7 of status (rx[3]) is BUSY, bit 5 is CRC error */
		if (!(focaltech_rx[3] & 0xa0)) {
			uint16_t expected = focaltech_crc(focaltech_rx + data_offset, length);
			uint16_t received = (uint16_t)(focaltech_rx[data_offset + length] |
			    focaltech_rx[data_offset + length + 1] << 8);
			if (expected == received) {
				memcpy(data, focaltech_rx + data_offset, length);
				sleep_us(150);		/* minimum CS-high time in the vendor driver */
				return true;
			}
		}
		sleep_us(150);
	}
	return false;
}

/*
 * Vendor fts_write: no CRC on TX. For a 1-byte "command" (length == 0) the frame is just the
 * four header bytes { addr, 0x00, 0x00, 0x00 } with no dummy and no data. For length > 0 the
 * three dummy bytes precede the data: { addr, 0x00, lenH, lenL, 0, 0, 0, data... }. The chip
 * drives the "status" byte on rx[3] -- bits 7 and 5 set mean busy or CRC error (vendor retries
 * SPI_RETRY_NUMBER = 3 times on failure before giving up).
 */
static bool
focaltech_write(uint8_t address, const uint8_t *data, uint32_t length)
{
	uint32_t transfer_length;

	if (length == 0) {
		transfer_length = 4;
	} else {
		transfer_length = 7 + length;
	}
	if (transfer_length > sizeof(focaltech_tx)) {
		return false;
	}
	memset(focaltech_tx, 0, transfer_length);
	focaltech_tx[0] = address;
	focaltech_tx[1] = FOCALTECH_WRITE_CTRL;
	focaltech_tx[2] = (uint8_t)(length >> 8);
	focaltech_tx[3] = (uint8_t)length;
	if (data && length > 0) {
		memcpy(focaltech_tx + 7, data, length);
	}
	for (int attempt = 0; attempt < 3; attempt++) {
		if (spi_transfer(focaltech_tx, focaltech_rx, transfer_length) &&
		    !(focaltech_rx[3] & 0xa0)) {
			sleep_us(150);
			return true;
		}
		sleep_us(150);
	}
	return false;
}

/*
 * Vendor fts_fwupg_get_boot_state() reads the chip's "boot ID" via the FT boot-ROM protocol:
 *   TX = { 0x55, 0xAA }                 (FTS_CMD_START1, FTS_CMD_START2 -- enter boot)
 *   wait FTS_CMD_START_DELAY = 12 ms
 *   TX = { 0x90 }, RX = 2 bytes         (FTS_CMD_READ_ID, incell cmd length)
 * If the two RX bytes are the chip's boot ID (0x56 0x62 for FT3680), firmware is present and
 * valid in flash but has not yet been started; the host must issue FTS_CMD_FIRMWARE_START
 * (0x08) to make the boot ROM jump to it.
 */
#define FTS_CMD_START1              0x55
#define FTS_CMD_START2              0xaa
#define FTS_CMD_START_DELAY         12		/* ms, between enter-boot and first command */
#define FTS_CMD_READ_BOOT_ID        0x90

/* Boot-ROM commands for firmware (pramboot) upload, from focaltech_3658u/focaltech_flash.h. */
#define FTS_ROMBOOT_CMD_SET_PRAM_ADDR   0xad
#define FTS_ROMBOOT_CMD_WRITE           0xae
#define FTS_ROMBOOT_CMD_START_APP       0x08
#define FTS_FLASH_PACKET_LENGTH         32		/* bytes per PRAM write */

#include "redwood_focaltech_fw.h"

/*
 * Vendor fts_read_bootid(): send { 0x55, 0xAA } as a 2-byte framed write (treated as a write of
 * one data byte 0xAA to register 0x55), wait 12 ms, then do a framed read of 2 bytes at
 * register 0x90. The chip answers with its ROM ID (0x56 0x62 for FT3680) if it is sitting in
 * the boot ROM waiting for a firmware download.
 */
static bool
focaltech_read_boot_id(uint8_t *high, uint8_t *low)
{
	uint8_t aa = FTS_CMD_START2;
	uint8_t out[2] = { 0 };
	bool ok;

	if (!focaltech_write(FTS_CMD_START1, &aa, 1)) {
		return false;
	}
	sleep_ms(12);			/* FTS_CMD_START_DELAY */
	ok = focaltech_read(FTS_CMD_READ_BOOT_ID, out, 2);
	*high = out[0];
	*low  = out[1];
	return ok;
}

/* Vendor fts_wait_tp_to_valid: poll chip ID for up to TIMEOUT_READ_REG (1 s) at 200 ms steps. */
static bool
focaltech_wait_valid(uint8_t *high, uint8_t *low)
{
	for (int attempt = 0; attempt < 5; attempt++) {
		if (focaltech_read(FOCALTECH_CHIP_ID_H, high, 1) &&
		    focaltech_read(FOCALTECH_CHIP_ID_L, low, 1) &&
		    *high == 0x56 && *low == 0x62) {
			printf("focaltech: FT3680 runtime ID valid after %d attempt(s)\n", attempt + 1);
			return true;
		}
		sleep_ms(200);			/* vendor INTERVAL_READ_REG */
	}
	return false;
}

/*
 * Reset the chip and send 0x55/0xAA to put it in boot ROM, as vendor fts_get_ic_information
 * does. Try up to 3 times with escalating settle delays (12, 20, 28 ms). Returns true if the
 * chip is sitting in its boot ROM, with { 0x56, 0x62 } read back from the boot-ID register.
 */
static bool
focaltech_enter_boot_rom(void)
{
	uint8_t boot_high = 0, boot_low = 0;

	for (int cnt = 0; cnt < 3; cnt++) {
		gpio_set(PIN_RESET, false);
		sleep_ms(1);
		gpio_set(PIN_RESET, true);
		sleep_ms(FTS_CMD_START_DELAY + cnt * 8);
		if (focaltech_read_boot_id(&boot_high, &boot_low) &&
		    boot_high == 0x56 && boot_low == 0x62) {
			printf("focaltech: in boot ROM after reset %d\n", cnt);
			return true;
		}
		printf("focaltech: boot ID attempt %d: %02x %02x (not 56 62)\n", cnt, boot_high, boot_low);
	}
	return false;
}

/*
 * Download the embedded firmware (pramboot + application) into the chip's PRAM via the boot
 * ROM's SPI_V2 upload protocol. Each 32-byte packet is sent as two framed writes: first set
 * the PRAM destination address (SET_PRAM_ADDR + 3 BE address bytes), then WRITE + data bytes.
 */
static bool
focaltech_write_pram(const uint8_t *buf, uint32_t len)
{
	uint8_t addr_bytes[3];

	for (uint32_t offset = 0; offset < len; offset += FTS_FLASH_PACKET_LENGTH) {
		uint32_t chunk = FTS_FLASH_PACKET_LENGTH;
		if (offset + chunk > len) {
			chunk = len - offset;
		}
		addr_bytes[0] = (uint8_t)(offset >> 16);
		addr_bytes[1] = (uint8_t)(offset >> 8);
		addr_bytes[2] = (uint8_t)offset;
		if (!focaltech_write(FTS_ROMBOOT_CMD_SET_PRAM_ADDR, addr_bytes, 3)) {
			printf("focaltech: PRAM address write failed at offset %u\n", offset);
			return false;
		}
		if (!focaltech_write(FTS_ROMBOOT_CMD_WRITE, buf + offset, chunk)) {
			printf("focaltech: PRAM data write failed at offset %u\n", offset);
			return false;
		}
	}
	return true;
}

static bool
focaltech_start_app(void)
{
	uint8_t cmd = FTS_ROMBOOT_CMD_START_APP;

	return focaltech_write(cmd, NULL, 0);
}

/*
 * Full chip bring-up: look for an already-running firmware; if none, reset to boot ROM and
 * download the embedded firmware into PRAM; then start it and wait for runtime to come up.
 */
static bool
focaltech_probe(bool verbose)
{
	uint8_t high = 0, low = 0;

	/* Maybe firmware is already running (previous session, warm boot). */
	if (focaltech_wait_valid(&high, &low)) {
		return true;
	}

	if (!focaltech_enter_boot_rom()) {
		if (verbose) {
			printf("focaltech: chip not in boot ROM; last read runtime %02x %02x\n", high, low);
		}
		return false;
	}

	printf("focaltech: downloading %u bytes of firmware to PRAM...\n", FOCALTECH_FW_LENGTH);
	if (!focaltech_write_pram(focaltech_fw, FOCALTECH_FW_LENGTH)) {
		printf("focaltech: PRAM download failed\n");
		return false;
	}
	printf("focaltech: PRAM download complete; starting application\n");
	if (!focaltech_start_app()) {
		printf("focaltech: start-app command failed\n");
		return false;
	}
	sleep_ms(300);			/* firmware needs time to initialize all peripherals */

	if (focaltech_wait_valid(&high, &low)) {
		return true;
	}
	if (verbose) {
		printf("focaltech: firmware did not come up (last runtime read %02x %02x)\n", high, low);
	}
	return false;
}

/* ---- FocalTech touch reports --------------------------------------------------------------------- */

/*
 * Per-chip report layout for FT3680 (vendor focaltech_core.c, FTS_TOUCH_OFF_* offsets):
 *   report[0] = gesture (0 outside gesture mode)
 *   report[1] = (chip_type << 4) | finger_count
 *   report[2 + 6*i + 0] = E_XH:  bits 7..6 = event (0 = down, 1 = up, 2 = contact)
 *                                bits 3..0 = x bits 14..11
 *   report[2 + 6*i + 1] = XL:    x bits 10..3
 *   report[2 + 6*i + 2] = ID_YH: bits 7..4 = finger id, bits 3..0 = y bits 14..11
 *   report[2 + 6*i + 3] = YL:    y bits 10..3
 *   report[2 + 6*i + 4] = PRE:   bits 7..5 = x bits 2..0, bits 4..2 = y bits 2..0
 *                                (also the pressure byte -- the lowest bits of each 15-bit axis
 *                                 are packed in with it)
 *   report[2 + 6*i + 5] = AREA:  touch area / major
 * Coordinates are 15-bit "super-high resolution" sub-pixel values; the touch panel's actual
 * extent is reported through the device tree as x_max/y_max (10800 x 24000 for redwood).
 */
#define FOCALTECH_EVENT_DOWN        0
#define FOCALTECH_EVENT_UP          1
#define FOCALTECH_EVENT_CONTACT     2

static struct {
	bool down, seen;
	uint32_t x, y;
} focaltech_contact[FOCALTECH_MAX_TOUCHES];

static void
focaltech_contact_update(uint32_t id, uint32_t event, uint32_t x, uint32_t y,
    uint32_t max_report_x, uint32_t max_report_y)
{
	static unsigned shown;

	if (id >= FOCALTECH_MAX_TOUCHES || max_report_x == 0 || max_report_y == 0) {
		return;
	}
	if (event == FOCALTECH_EVENT_UP) {
		focaltech_contact[id].seen = true;
		if (focaltech_contact[id].down) {
			focaltech_contact[id].down = false;
			redwood_touch_post(REDWOOD_TOUCH_UP, id, focaltech_contact[id].x, focaltech_contact[id].y);
		}
		return;
	}
	seen.touches++;
	switch (flip) {
	case 1: x = max_report_x - 1 - x; break;
	case 2: y = max_report_y - 1 - y; break;
	case 3: x = max_report_x - 1 - x; y = max_report_y - 1 - y; break;
	}
	x = (uint32_t)(((uint64_t)x * 65535) / (max_report_x - 1));
	y = (uint32_t)(((uint64_t)y * 65535) / (max_report_y - 1));
	seen.last_x = x;
	seen.last_y = y;
	focaltech_contact[id].seen = true;
	if (!focaltech_contact[id].down) {
		focaltech_contact[id].down = true;
		redwood_touch_post(REDWOOD_TOUCH_DOWN, id, x, y);
	} else if (focaltech_contact[id].x != x || focaltech_contact[id].y != y) {
		redwood_touch_post(REDWOOD_TOUCH_MOVE, id, x, y);
	}
	focaltech_contact[id].x = x;
	focaltech_contact[id].y = y;
	if (shown++ < 8) {
		printf("focaltech: finger %u %s at %u, %u\n", id,
		    event == FOCALTECH_EVENT_DOWN ? "down" : "move", x, y);
	}
}

/* Panel size in the FT3680 super-high-resolution reports (device tree x_max/y_max on redwood). */
static uint32_t focaltech_max_x = 10800, focaltech_max_y = 24000;

static void
focaltech_report(const uint8_t *report)
{
	uint32_t count = report[1] & 0x0f;

	for (uint32_t i = 0; i < FOCALTECH_MAX_TOUCHES; i++) {
		focaltech_contact[i].seen = false;
	}
	for (uint32_t i = 0; i < FOCALTECH_MAX_TOUCHES; i++) {
		const uint8_t *touch = report + 2 + 6 * i;
		uint32_t id = (touch[2] >> 4) & 0x0f;
		uint32_t event, x, y;

		if (id >= FOCALTECH_MAX_TOUCHES) {
			break;			/* no more valid slots in this report */
		}
		event = (touch[0] >> 6) & 0x3;
		x = ((uint32_t)(touch[0] & 0x0f) << 11) | ((uint32_t)touch[1] << 3) |
		    ((uint32_t)touch[4] >> 5);
		y = ((uint32_t)(touch[2] & 0x0f) << 11) | ((uint32_t)touch[3] << 3) |
		    (((uint32_t)touch[4] & 0x1c) >> 2);
		focaltech_contact_update(id, event, x, y, focaltech_max_x, focaltech_max_y);
	}
	(void)count;
	/* Fingers not in this report are up (vendor driver uses a bitmap; we remember explicitly). */
	for (uint32_t i = 0; i < FOCALTECH_MAX_TOUCHES; i++) {
		if (!focaltech_contact[i].seen && focaltech_contact[i].down) {
			focaltech_contact[i].down = false;
			redwood_touch_post(REDWOOD_TOUCH_UP, i, focaltech_contact[i].x, focaltech_contact[i].y);
		}
	}
}

static void
focaltech_poll(void)
{
	static uint8_t report[FOCALTECH_TOUCH_REPORT_LEN];
	static unsigned bad;

	seen.polls++;
	if (!focaltech_read(FOCALTECH_TOUCH_DATA, report, sizeof(report))) {
		seen.bad++;
		if (bad++ < 8) {
			printf("focaltech: SPI read failed for touch report\n");
		}
		return;
	}
	seen.reports++;
	focaltech_report(report);
}
