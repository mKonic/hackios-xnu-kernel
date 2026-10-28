/* ------------------------------------------------------------------------ */
/* UFS block service (ufs=2): the kernel reads the phone's storage through  */
/* this loader, and this loader through Qualcomm's own UFS driver           */

/*
 * The kernel has no driver for the phone's storage, and does not need one: UEFI never exits here,
 * Qualcomm's UFS driver (UFSDxe) has the controller, its PHY, clocks and power up and stays
 * resident, and this loader already serves the kernel from core 0 while the kernel runs on another
 * (the battery). So the kernel asks for blocks through a page both can see, and core 0 reads them
 * with that driver's ReadBlocks into a buffer both can see. The kernel's side is
 * bsd/dev/redwood_ufs.c; it gets the page's address as ufs_shared_pa= on its command line.
 *
 * Reads, and -- with ufs_write=1 on the command line -- writes into one partition and nowhere
 * else: the port's own darwin, proved by its GPT entry (name, both GUIDs, first and last LBA, its
 * logical unit), and in it only past everything the build writes from the computer, so that no
 * answer to the kernel can reach Android's partitions, the iOS volume or the cryptexes. The
 * refusals are here, in the kernel (bsd/dev/redwood_ufs.c) and in UEFI's own driver, which gives
 * out a partition's BlockIo and bounds it: a request has to pass all three.
 *
 * Each side writes its own cache lines only: the kernel the request, core 0 the answer (and the
 * table, once, before the kernel starts). A line one side cleans out of its cache therefore never
 * carries a stale copy of what the other side wrote into it. Into the data buffer core 0 writes
 * only what it has read back after a write of its own, so after the controller's DMA there is
 * nothing of its own in its cache to write back over the data; the kernel, which fills the buffer
 * for a write, cleans its own lines of it before it asks.
 */
#define BLOCK_MAGIC        0x4b434f4c42574452ULL       /* "RDWBLOCK" */
#define BLOCK_VERSION      3
#define BLOCK_MAX_PARTS    160
#define BLOCK_MAX_UNITS    16
#define BLOCK_PART_UNIT    1u                          /* the entry is a whole logical unit, not a partition */
#define BLOCK_OP_READ      0u
#define BLOCK_OP_WRITE     1u

/*
 * The one partition a write may reach, and from which of its blocks. darwin is the port's own
 * 20 GiB at the end of logical unit 0 (README, "The port's partition"); what the build puts there
 * from the computer (tools/darwin-write.py) is the iOS volume at its start and the two cryptexes
 * behind it, and nothing of this port reads or writes the rest -- so the data of iOS lives there
 * and a write is refused before that block. The identity is the whole of darwin-write.py's: the
 * same partition, named the same, of the same size, in the same place, on logical unit 0.
 */
#define BLOCK_WRITE_FIRST  2042368ULL                  /* darwin's blocks of 4096: past both cryptexes */
#define BLOCK_DARWIN_FIRST 25374720ULL                 /* its first and last LBA of unit 0, as the GPT has them */
#define BLOCK_DARWIN_LAST  30617594ULL
#define BLOCK_DARWIN_TYPE  { 0x7c3457ef, 0x0000, 0x11aa, { 0xaa, 0x11, 0x00, 0x30, 0x65, 0x43, 0xec, 0xac } }
#define BLOCK_DARWIN_UNIQ  { 0xfd6854ad, 0x24aa, 0x4fc3, { 0x94, 0xfe, 0x29, 0x01, 0x16, 0x85, 0x8d, 0x68 } }

/*
 * The partitions first, then the logical units they are on (BLOCK_PART_UNIT, named unit0, unit1,
 * ... in the order UEFI lists them). Each partition says which of those it is on and where it
 * begins there, so that a reader of the units themselves -- the kernel's own UFS driver -- finds
 * it; a unit says its logical unit number if its device path does.
 */
struct loader_block_part {
	char name[36];
	uint32_t block_size;
	uint64_t blocks;
	uint64_t start_lba;            /* where it begins on its unit, in the unit's blocks (0 for a unit) */
	uint16_t unit;                 /* the unit it is on, its number among the units; 0xffff: not known */
	uint8_t lun;                   /* a unit's logical unit number as its device path says; 0xff: it does not */
	uint8_t flags;                 /* BLOCK_PART_UNIT */
	uint32_t reserved;
};

struct loader_block {
	/* written by the loader, once, before the kernel starts */
	uint64_t magic;
	uint32_t version, size;
	uint32_t parts, buffer_size;
	uint64_t buffer_pa;
	/*
	 * Which partition takes writes this run, as its index among part[] plus one (0: none), and
	 * from which of its blocks. They are here, in what was padding before ask, so that the page
	 * stays the size and the version it was: one phone is shared between working copies, and a
	 * kernel built before writes existed must go on reading through a loader built after them,
	 * and the other way about -- a kernel that does not know these fields leaves ask.op zero,
	 * which is a read, and a loader that does not know them leaves these zero, which is no write.
	 */
	uint32_t write_part_plus_one;
	uint32_t write_reserved;
	uint64_t write_first;
	/* written by the kernel: what it wants, then request is advanced */
	struct {
		volatile uint32_t request;
		uint32_t part;
		uint64_t lba, count;           /* in the partition's own blocks */
		uint32_t op;                   /* BLOCK_OP_READ, BLOCK_OP_WRITE */
		uint32_t reserved;
	} ask __attribute__((aligned(128)));
	/* written by the loader: answered becomes request when the data is in the buffer */
	struct {
		volatile uint32_t answered;
		uint32_t reserved;
		int64_t status;                /* the EFI status of the read, 0 when it went well */
		uint64_t served, failed, bytes, ticks;
	} answer __attribute__((aligned(128)));
	struct loader_block_part part[BLOCK_MAX_PARTS] __attribute__((aligned(128)));
	/*
	 * Written by the loader before the kernel starts, with ufs_native= on the command line: where
	 * the kernel may put a transfer request list of its own into the UFS host controller for the
	 * run (hc_area, below), and UEFI's, which the loader puts back. hc_pa 0: it may not.
	 */
	struct {
		uint64_t hc_pa;                /* the controller's registers */
		uint64_t area_pa;              /* the list (1 KB, at the start) and command descriptors */
		uint32_t area_size, ucd_offset, ucd_size, slots;
		uint32_t uefi_utrlba, uefi_utrlbau;
	} native __attribute__((aligned(128)));
	/* written by the loader during a run: it takes the controller back (a hung kernel); send nothing more */
	struct {
		volatile uint32_t take_back;
	} loader_native __attribute__((aligned(128)));
	/* written by the kernel: its list is in the controller (for the loader's log) */
	struct {
		volatile uint32_t list_in;
		uint32_t switched_in, switched_out;
		volatile uint32_t cnr_at_switch;       /* UTRLCNR when the kernel's list went in */
	} kernel_native __attribute__((aligned(128)));
};

_Static_assert(__builtin_offsetof(struct loader_block, ask) == 128,
    "the page must stay the size it was: the fields before ask have to fit in what was padding");

/* (8 KB is as far as this target aligns; the kernel maps whole 16 KB pages around both) */
static struct loader_block block_shared __attribute__((aligned(0x2000)));
static void block_io2_probe(efi_handle h, efi_block_io *bio);
static int block_probe_requested;       /* ufs_probe=1: block_io2_probe() at block_start() */
static efi_block_io *block_bios[BLOCK_MAX_PARTS];
static int block_enabled;
static int block_write_requested;       /* ufs_write=1 on the command line */
static int block_write_part = -1;       /* the only partition a write may reach; -1: none, writes refused */
static uint64_t block_writes, block_writes_refused, block_write_bytes;

/* the bytes of a device path before its end node */
static uint64_t
device_path_bytes(const efi_device_path *dp)
{
	const uint8_t *p = (const uint8_t *)dp;
	uint64_t n = 0;

	for (int nodes = 0; nodes < 64; nodes++) {
		uint64_t len = p[n + 2] | (uint64_t)p[n + 3] << 8;

		if (p[n] == 0x7f || len < 4) {
			break;
		}
		n += len;
	}
	return n;
}

/*
 * A unit's device path, a node at a time ("ufs: unit N: path TYPE.SUBTYPE[data] ..."), and its
 * logical unit number if a UFS node (messaging, subtype 0x19: target, then LUN) says it.
 */
static uint8_t
device_path_print(uint64_t unit, const efi_device_path *dp)
{
	const uint8_t *p = (const uint8_t *)dp;
	uint64_t n = 0, total = device_path_bytes(dp);
	uint8_t lun = 0xff;

	print("ufs: unit %d: path", unit);
	while (n < total) {
		uint64_t len = p[n + 2] | (uint64_t)p[n + 3] << 8;

		print(" %x.%x[", (uint64_t)p[n], (uint64_t)p[n + 1]);
		for (uint64_t i = 4; i < len && i < 4 + 24; i++) {
			print("%s%x", (uint64_t)p[n + i] < 16 ? "0" : "", (uint64_t)p[n + i]);
		}
		print("%s]", len > 4 + 24 ? "..." : "");
		if (p[n] == 3 && p[n + 1] == 0x19 && len >= 6) {
			lun = p[n + 5];
		}
		n += len;
	}
	print(lun == 0xff ? "\n" : " (LUN %d)\n", (uint64_t)lun);
	return lun;
}

/*
 * The UFS host controller, for the kernel's own driver (ufs_native= on the command line; without it
 * this loader never touches the controller's registers). The kernel may, for the length of a run,
 * stop the controller's transfer request list, point it at a list of its own in hc_area (memory of
 * this loader, so that putting UEFI's back never depends on the kernel's memory), and start it
 * again; UEFI's list is one slot and its command descriptor, with no room for another request.
 * This loader keeps UEFI's list address (UTRLBA/UTRLBAU) from before the first run and checks it,
 * and puts it back if need be, on every way back from a run (hc_take_back) before anything here
 * touches the storage; it reads for the kernel (block_poll) and writes the log (write_logs) only
 * while UEFI's list is the one in the controller.
 */
#define HC_PA              0x1d84000ULL        /* ufshc@1d84000, "ufs_mem", in this phone's Android device tree */
#define HC_CAP             0x00
#define HC_VER             0x08
#define HC_IS              0x20
#define HC_IE              0x24
#define HC_HCS             0x30
#define HC_HCE             0x34
#define HC_UTRLBA          0x50
#define HC_UTRLBAU         0x54
#define HC_UTRLDBR         0x58
#define HC_UTRLCLR         0x5c
#define HC_UTRLRSR         0x60
#define HC_UTRLCNR         0x64
#define HC_UTMRLDBR        0x78
#define HC_SLOTS           32
#define HC_UCD_OFFSET      0x1000
#define HC_UCD_SIZE        0x800
#define HC_AREA_SIZE       (HC_UCD_OFFSET + HC_SLOTS * HC_UCD_SIZE)

static uint8_t hc_area[HC_AREA_SIZE] __attribute__((aligned(0x2000)));
static int hc_requested;                /* ufs_native= on the command line */
static int hc_known;                    /* UEFI's list address is kept here: check it, put it back */
static uint32_t hc_uefi_lo, hc_uefi_hi;
static int hc_refused_reads;

static inline uint32_t
hc_rd(uint32_t off)
{
	return *(volatile uint32_t *)(uintptr_t)(HC_PA + off);
}

static inline void
hc_wr(uint32_t off, uint32_t v)
{
	__asm__ volatile ("dsb sy" ::: "memory");
	*(volatile uint32_t *)(uintptr_t)(HC_PA + off) = v;
	__asm__ volatile ("dsb sy" ::: "memory");
}

static uint64_t
hc_ms_since(uint64_t then)
{
	return (READ_SYSREG(CNTVCT_EL0) - then) * 1000 / READ_SYSREG(CNTFRQ_EL0);
}

/* UEFI's list in, and running: what UEFI's driver needs to read or write */
static int
hc_list_is_uefis(void)
{
	return !hc_known || (hc_rd(HC_UTRLBA) == hc_uefi_lo && hc_rd(HC_UTRLBAU) == hc_uefi_hi &&
	       (hc_rd(HC_UTRLRSR) & 1) != 0);
}

static void
hc_print_state(const char *when)
{
	print("ufs: %s: list %x:%x (UEFI's %x:%x), run %x, doorbell %x, HCS %x, IS %x, IE %x, completion %x, "
	    "task doorbell %x\n", when, (uint64_t)hc_rd(HC_UTRLBAU), (uint64_t)hc_rd(HC_UTRLBA), (uint64_t)hc_uefi_hi,
	    (uint64_t)hc_uefi_lo, (uint64_t)hc_rd(HC_UTRLRSR), (uint64_t)hc_rd(HC_UTRLDBR), (uint64_t)hc_rd(HC_HCS),
	    (uint64_t)hc_rd(HC_IS), (uint64_t)hc_rd(HC_IE), (uint64_t)hc_rd(HC_UTRLCNR), (uint64_t)hc_rd(HC_UTMRLDBR));
}

/*
 * Whatever the kernel left: UEFI's list back in the controller. The kernel is told to send nothing
 * more (it looks before every doorbell, with interrupts off), given 50 ms, then what is in flight
 * is waited for (1 s), cleared if it does not finish (UTRLCLR: every slot of that list is the
 * kernel's), the list stopped, UEFI's address written back, the list started, all read back.
 */
static void
hc_take_back(const char *when)
{
	uint32_t lo, hi, rsr, db;
	uint64_t t;

	if (!hc_known) {
		return;
	}
	lo = hc_rd(HC_UTRLBA);
	hi = hc_rd(HC_UTRLBAU);
	rsr = hc_rd(HC_UTRLRSR);
	db = hc_rd(HC_UTRLDBR);
	if (lo == hc_uefi_lo && hi == hc_uefi_hi && (rsr & 1)) {
		print("ufs: %s: UEFI's transfer list is in the controller (%x), running, doorbell %x\n", when, (uint64_t)lo,
		    (uint64_t)db);
		return;
	}
	print("ufs: %s: the controller has list %x:%x (run %x, doorbell %x), not UEFI's %x:%x: taking it back\n", when,
	    (uint64_t)hi, (uint64_t)lo, (uint64_t)rsr, (uint64_t)db, (uint64_t)hc_uefi_hi, (uint64_t)hc_uefi_lo);
	block_shared.loader_native.take_back = 1;
	clean_dcache((uint64_t)(uintptr_t)&block_shared.loader_native,
	    (uint64_t)(uintptr_t)&block_shared.loader_native + sizeof(block_shared.loader_native));
	for (t = READ_SYSREG(CNTVCT_EL0); hc_ms_since(t) < 50;) {
	}
	for (t = READ_SYSREG(CNTVCT_EL0); (db = hc_rd(HC_UTRLDBR)) != 0 && hc_ms_since(t) < 1000;) {
	}
	if (db != 0) {
		print("ufs: %s: doorbell %x still set after 1 s\n", when, (uint64_t)db);
		if (hc_rd(HC_UTRLBA) != hc_uefi_lo || hc_rd(HC_UTRLBAU) != hc_uefi_hi) {
			print("ufs: %s: clearing the kernel's slots (UTRLCLR)\n", when);
			hc_wr(HC_UTRLCLR, 0);
			for (t = READ_SYSREG(CNTVCT_EL0); (db = hc_rd(HC_UTRLDBR)) != 0 && hc_ms_since(t) < 1000;) {
			}
			print("ufs: %s: doorbell %x after the clear\n", when, (uint64_t)db);
		}
	}
	/* the completion notifications the kernel's requests left (it says what there was when its list went in) */
	flush_dcache((uint64_t)(uintptr_t)&block_shared.kernel_native,
	    (uint64_t)(uintptr_t)&block_shared.kernel_native + sizeof(block_shared.kernel_native));
	if (block_shared.kernel_native.switched_in != 0) {
		uint32_t cnr = hc_rd(HC_UTRLCNR), ours = cnr & ~block_shared.kernel_native.cnr_at_switch;

		if (ours != 0) {
			hc_wr(HC_UTRLCNR, ours);
		}
		print("ufs: %s: completion %x, at the kernel's switch %x: cleared %x, now %x\n", when, (uint64_t)cnr,
		    (uint64_t)block_shared.kernel_native.cnr_at_switch, (uint64_t)ours, (uint64_t)hc_rd(HC_UTRLCNR));
	}
	hc_wr(HC_UTRLRSR, 0);
	for (t = READ_SYSREG(CNTVCT_EL0); (hc_rd(HC_UTRLRSR) & 1) && hc_ms_since(t) < 100;) {
	}
	hc_wr(HC_UTRLBA, hc_uefi_lo);
	hc_wr(HC_UTRLBAU, hc_uefi_hi);
	hc_wr(HC_UTRLRSR, 1);
	for (t = READ_SYSREG(CNTVCT_EL0); hc_ms_since(t) < 50;) {
	}
	hc_print_state(when);
	print("ufs: %s: UEFI's list %s\n", when, hc_list_is_uefis() ? "put back" : "NOT PUT BACK: the storage is not used");
}

/*
 * Before a run (ufs_native=): what the controller looks like, and UEFI's list address kept -- the
 * first time; later it must be the same (or this loader's own area, which is then taken back).
 */
static int
hc_init(void)
{
	uint32_t lo, hi, area = (uint32_t)(uintptr_t)hc_area;

	if (!hc_requested) {
		return 0;
	}
	lo = hc_rd(HC_UTRLBA);
	hi = hc_rd(HC_UTRLBAU);
	print("ufs: controller at %x: CAP %x, VER %x, HCE %x, HCS %x, IS %x, IE %x, list %x:%x, run %x, doorbell %x\n",
	    HC_PA, (uint64_t)hc_rd(HC_CAP), (uint64_t)hc_rd(HC_VER), (uint64_t)hc_rd(HC_HCE), (uint64_t)hc_rd(HC_HCS),
	    (uint64_t)hc_rd(HC_IS), (uint64_t)hc_rd(HC_IE), (uint64_t)hi, (uint64_t)lo, (uint64_t)hc_rd(HC_UTRLRSR),
	    (uint64_t)hc_rd(HC_UTRLDBR));
	if (!hc_known) {
		if (hi == 0 && lo == area) {
			print("ufs: the controller has this loader's own area for its list before any run: left alone\n");
			return 0;
		}
		hc_uefi_lo = lo;
		hc_uefi_hi = hi;
		hc_known = 1;
		print("ufs: UEFI's transfer list is at %x:%x; it is put back after every run\n", (uint64_t)hi, (uint64_t)lo);
	} else if (lo != hc_uefi_lo || hi != hc_uefi_hi) {
		if (hi == 0 && lo == area) {
			hc_take_back("before the run");
		} else {
			print("ufs: UEFI's list moved from %x:%x to %x:%x: that is UEFI's now\n", (uint64_t)hc_uefi_hi,
			    (uint64_t)hc_uefi_lo, (uint64_t)hi, (uint64_t)lo);
			hc_uefi_lo = lo;
			hc_uefi_hi = hi;
		}
	}
	return hc_list_is_uefis();
}

/*
 * The firmware's configuration tables, by GUID (an ACPI or device tree table among them would say
 * where the storage controller is).
 */
static void
block_tables(void)
{
	for (uint64_t i = 0; ST->tables != NULL && i < ST->nr_tables && i < 32; i++) {
		const efi_guid *g = &ST->tables[i].guid;

		print("ufs: firmware table %d: %x-%x-%x-%x%x-%x%x%x%x%x%x at %x\n", i, (uint64_t)g->a, (uint64_t)g->b,
		    (uint64_t)g->c, (uint64_t)g->d[0], (uint64_t)g->d[1], (uint64_t)g->d[2], (uint64_t)g->d[3],
		    (uint64_t)g->d[4], (uint64_t)g->d[5], (uint64_t)g->d[6], (uint64_t)g->d[7],
		    (uint64_t)(uintptr_t)ST->tables[i].table);
	}
}

/*
 * Is this partition the one a write may reach? Everything about it has to be what it was when the
 * port was set up; one field out of place and nothing is written this run. The name UEFI reports
 * is already in bp->name (ufs_name), in ASCII.
 */
static int
block_is_darwin(const efi_partition_info *info, const efi_block_io *bio, const struct loader_block_part *bp)
{
	static const efi_guid type = BLOCK_DARWIN_TYPE, uniq = BLOCK_DARWIN_UNIQ;
	static const char name[] = "darwin";
	const char *why = NULL;

	for (unsigned i = 0; i < sizeof(name); i++) {
		if (bp->name[i] != name[i]) {
			return 0;               /* another partition: nothing to say about it */
		}
	}
	if (memcmp(&info->type_guid, &type, sizeof(type)) != 0) {
		why = "its type GUID is not the one it had";
	} else if (memcmp(&info->unique_guid, &uniq, sizeof(uniq)) != 0) {
		why = "its unique GUID is not the one it had";
	} else if (info->start_lba != BLOCK_DARWIN_FIRST || info->end_lba != BLOCK_DARWIN_LAST) {
		why = "it does not begin and end where it did";
	} else if (bp->unit != 0) {
		/* the first unit UEFI lists: this firmware's device paths say no logical unit number
		 * (device_path_print finds no 3.19 node), so bp->lun is 0xff for every unit */
		why = "it is not on the first logical unit";
	} else if (bp->block_size != 4096 || bp->blocks != BLOCK_DARWIN_LAST - BLOCK_DARWIN_FIRST + 1) {
		why = "its blocks are not what they were";
	} else if (bio->media->read_only) {
		why = "UEFI gives it out read only";
	}
	if (why != NULL) {
		print("ufs: darwin: no write will be served: %s\n", why);
		return 0;
	}
	print("ufs: darwin (LBA %d..%d of unit 0): its blocks from %d on may be written this run\n",
	    info->start_lba, info->end_lba, BLOCK_WRITE_FIRST);
	return 1;
}

static void
block_start(void)
{
	static const efi_guid part_guid = EFI_PARTITION_INFO_GUID;
	static const efi_guid blk_guid = EFI_BLOCK_IO_GUID;
	efi_handle *handles;
	uint64_t count;

	static const efi_guid dp_guid = EFI_DEVICE_PATH_GUID;
	static struct {
		efi_block_io *bio;
		const efi_device_path *path;
		uint64_t path_bytes;
		uint8_t lun;
	} units[BLOCK_MAX_UNITS];
	uint64_t nunits = 0;

	memset(&block_shared, 0, sizeof(block_shared));
	block_tables();
	/* the logical units: the block devices that are not partitions of another */
	if (!EFI_ERROR(BS->locate_handle_buffer(ByProtocol, &blk_guid, NULL, &count, &handles))) {
		for (uint64_t i = 0; i < count && nunits < BLOCK_MAX_UNITS; i++) {
			efi_block_io *bio;
			efi_device_path *dp;

			if (EFI_ERROR(BS->handle_protocol(handles[i], &blk_guid, (void **)&bio)) || bio->media->logical ||
			    bio->media->block_size == 0 || bio->media->block_size > BLOCK_BUFFER_SIZE ||
			    EFI_ERROR(BS->handle_protocol(handles[i], &dp_guid, (void **)&dp))) {
				continue;
			}
			units[nunits].bio = bio;
			units[nunits].path = dp;
			units[nunits].path_bytes = device_path_bytes(dp);
			units[nunits].lun = device_path_print(nunits, dp);
			nunits++;
		}
	}
	if (EFI_ERROR(BS->locate_handle_buffer(ByProtocol, &part_guid, NULL, &count, &handles))) {
		print("ufs: no partition information: the block service is off\n");
		return;
	}
	for (uint64_t i = 0; i < count && block_shared.parts + nunits < BLOCK_MAX_PARTS; i++) {
		struct loader_block_part *bp = &block_shared.part[block_shared.parts];
		efi_partition_info *info;
		efi_block_io *bio;
		efi_device_path *dp;

		if (EFI_ERROR(BS->handle_protocol(handles[i], &part_guid, (void **)&info)) || info->type != 2 ||
		    EFI_ERROR(BS->handle_protocol(handles[i], &blk_guid, (void **)&bio)) ||
		    bio->media->block_size == 0 || bio->media->block_size > BLOCK_BUFFER_SIZE) {
			continue;
		}
		ufs_name(info, bp->name, sizeof(bp->name));
		bp->block_size = bio->media->block_size;
		bp->blocks = bio->media->last_block + 1;
		bp->start_lba = info->start_lba;
		bp->unit = 0xffff;
		bp->lun = 0xff;
		/* its unit: the one whose device path its own begins with */
		if (!EFI_ERROR(BS->handle_protocol(handles[i], &dp_guid, (void **)&dp))) {
			uint64_t bytes = device_path_bytes(dp);

			for (uint64_t u = 0; u < nunits; u++) {
				if (units[u].path_bytes < bytes &&
				    memcmp(units[u].path, dp, units[u].path_bytes) == 0) {
					bp->unit = (uint16_t)u;
					bp->lun = units[u].lun;
				}
			}
		}
		if (block_write_requested && block_is_darwin(info, bio, bp)) {
			block_write_part = (int)block_shared.parts;
		}
		block_bios[block_shared.parts++] = bio;
		if (block_probe_requested && bp->name[0] == 'd' && bp->name[1] == 'a' && bp->name[2] == 'r' &&
		    bp->name[3] == 'w' && bp->name[4] == 'i' && bp->name[5] == 'n' && bp->name[6] == 0) {
			block_io2_probe(handles[i], bio);
		}
	}
	for (uint64_t u = 0; u < nunits; u++) {
		struct loader_block_part *bp = &block_shared.part[block_shared.parts];
		char *n = bp->name;

		n[0] = 'u', n[1] = 'n', n[2] = 'i', n[3] = 't';
		n[4] = (char)('0' + u / 10 % 10), n[5] = (char)('0' + u % 10), n[6] = 0;
		if (u < 10) {
			n[4] = n[5], n[5] = 0;
		}
		bp->block_size = units[u].bio->media->block_size;
		bp->blocks = units[u].bio->media->last_block + 1;
		bp->unit = (uint16_t)u;
		bp->lun = units[u].lun;
		bp->flags = BLOCK_PART_UNIT;
		block_bios[block_shared.parts++] = units[u].bio;
	}
	/* the kernel's own transfer list: this loader's area, if the controller is as UEFI keeps it */
	if (hc_known && hc_list_is_uefis() && hc_rd(HC_UTRLDBR) == 0 && (hc_rd(HC_HCE) & 1)) {
		memset(hc_area, 0, sizeof(hc_area));
		flush_dcache((uint64_t)(uintptr_t)hc_area, (uint64_t)(uintptr_t)hc_area + sizeof(hc_area));
		block_shared.native.hc_pa = HC_PA;
		block_shared.native.area_pa = (uint64_t)(uintptr_t)hc_area;
		block_shared.native.area_size = sizeof(hc_area);
		block_shared.native.ucd_offset = HC_UCD_OFFSET;
		block_shared.native.ucd_size = HC_UCD_SIZE;
		block_shared.native.slots = HC_SLOTS;
		block_shared.native.uefi_utrlba = hc_uefi_lo;
		block_shared.native.uefi_utrlbau = hc_uefi_hi;
		print("ufs: the kernel may put its own transfer list in, at %x (this loader's), for the run\n",
		    (uint64_t)(uintptr_t)hc_area);
	} else if (hc_requested) {
		print("ufs: the kernel's own transfer list is not offered: the controller is not as UEFI keeps it\n");
	}
	hc_refused_reads = 0;
	hc_log_skips = 0;
	/* per run, like the two above: this loader stays resident across runs, and a tally carried
	 * over from the run before is read as this one's */
	block_writes = block_writes_refused = block_write_bytes = 0;
	if (block_write_part >= 0) {
		block_shared.write_part_plus_one = (uint32_t)block_write_part + 1;
		block_shared.write_first = BLOCK_WRITE_FIRST;
	}
	block_shared.magic = BLOCK_MAGIC;
	block_shared.version = BLOCK_VERSION;
	block_shared.size = sizeof(block_shared);
	block_shared.buffer_size = BLOCK_BUFFER_SIZE;
	block_shared.buffer_pa = (uint64_t)(uintptr_t)block_buffer;
	clean_dcache((uint64_t)(uintptr_t)&block_shared, (uint64_t)(uintptr_t)&block_shared + sizeof(block_shared));
	block_enabled = 1;
	print("ufs: block service: %d partitions and %d units, page %x, buffer %x of %d KB\n",
	    (uint64_t)block_shared.parts - nunits, nunits, (uint64_t)(uintptr_t)&block_shared, block_shared.buffer_pa,
	    (uint64_t)(BLOCK_BUFFER_SIZE >> 10));
}

/*
 * Whether the storage could have more than one request in flight (ufs_probe=1): Block I/O 2
 * on the partitions, and whether its reads come back before they are done.  Eight 4 KB
 * reads at places 64 MB apart in "darwin", one after another through Block I/O and then all
 * eight at once through Block I/O 2, each into its own part of the buffer.  Before the kernel
 * runs (the UEFI timer still ticks), and only to say what there is.
 *
 * What it says on this phone: Block I/O 2 is there and the calls come back at once (eight in
 * 160 us, none done), but the eight are done only after 61.5 ms, against 1.0 ms one at a time
 * through Block I/O -- Qualcomm's driver finishes them from the UEFI timer, which besides does
 * not tick on core 0 while the kernel runs.  So one request at a time, synchronously.
 */
static void
block_io2_probe(efi_handle h, efi_block_io *bio)
{
	static const efi_guid blk2_guid = EFI_BLOCK_IO2_GUID;
	efi_status (EFIAPI *create_event)(uint32_t, uint64_t, void *, void *, void **) =
	    (efi_status (EFIAPI *)(uint32_t, uint64_t, void *, void *, void **))BS->create_event;
	efi_status (EFIAPI *check_event)(void *) = (efi_status (EFIAPI *)(void *))BS->check_event;
	static efi_block_io2_token tokens[8];
	efi_block_io2 *bio2 = NULL;
	uint64_t bs = bio->media->block_size, t0, t_sync, t_issue = 0, t_all, freq = READ_SYSREG(CNTFRQ_EL0);
	uint64_t returned_done = 0, pending, polls = 0;
	efi_status s;

	if (EFI_ERROR(BS->handle_protocol(h, &blk2_guid, (void **)&bio2)) || bio2 == NULL) {
		print("ufs: darwin: no Block I/O 2: one request at a time it is\n");
		return;
	}
	t0 = READ_SYSREG(CNTVCT_EL0);
	for (int i = 0; i < 8; i++) {
		s = bio->read_blocks(bio, bio->media->media_id, (uint64_t)i * (64ULL << 20) / bs + 7, bs,
		    block_buffer + i * 65536);
		if (EFI_ERROR(s)) {
			print("ufs: probe: Block I/O read failed %x\n", s);
			return;
		}
	}
	t_sync = READ_SYSREG(CNTVCT_EL0) - t0;
	for (int i = 0; i < 8; i++) {
		tokens[i].transaction_status = 0;
		if (EFI_ERROR(create_event(0, 8 /* TPL_CALLBACK */, NULL, NULL, &tokens[i].event))) {
			print("ufs: probe: no event\n");
			return;
		}
	}
	t0 = READ_SYSREG(CNTVCT_EL0);
	for (int i = 0; i < 8; i++) {
		/* other places than the synchronous reads, so that nothing the storage kept is read again */
		s = bio2->read_blocks_ex(bio2, bio2->media->media_id, (uint64_t)i * (64ULL << 20) / bs + 1031,
		    &tokens[i], bs, block_buffer + 1048576 + i * 65536);
		if (EFI_ERROR(s)) {
			print("ufs: probe: ReadBlocksEx %d failed %x\n", (uint64_t)i, s);
			return;
		}
		if (check_event(tokens[i].event) == 0) {
			returned_done++;        /* already signalled when the call came back */
		}
	}
	t_issue = READ_SYSREG(CNTVCT_EL0) - t0;
	pending = 8 - returned_done;
	while (pending > 0 && polls < 50000000) {
		pending = 0;
		for (int i = 0; i < 8; i++) {
			if (tokens[i].event != NULL && check_event(tokens[i].event) != 0) {
				pending++;
			} else if (tokens[i].event != NULL) {
				BS->close_event(tokens[i].event);
				tokens[i].event = NULL;
			}
		}
		polls++;
	}
	t_all = READ_SYSREG(CNTVCT_EL0) - t0;
	print("ufs: darwin: Block I/O 2 there. 8 random 4 KB reads: one at a time %d us; at once: the calls came back "
	    "after %d us (%d of them done by then), all done after %d us, %d still pending\n",
	    t_sync * 1000000 / freq, t_issue * 1000000 / freq, returned_done, t_all * 1000000 / freq, pending);
}

/* one request, if there is one: called from core 0's watch loop while the kernel runs */
static void
block_poll(void)
{
	uint64_t ask = (uint64_t)(uintptr_t)&block_shared.ask;
	uint32_t request, part, op;
	uint64_t lba, count, bytes, started;
	efi_status s = 0x8000000000000002ULL;     /* EFI_INVALID_PARAMETER: a request out of bounds */

	/* the kernel writes the request into memory past this core's cache: look at memory */
	flush_dcache(ask, ask + sizeof(block_shared.ask));
	request = block_shared.ask.request;
	if (request == block_shared.answer.answered) {
		return;
	}
	part = block_shared.ask.part;
	lba = block_shared.ask.lba;
	count = block_shared.ask.count;
	op = block_shared.ask.op;
	started = READ_SYSREG(CNTVCT_EL0);
	bytes = 0;
	if (part < block_shared.parts && count != 0 && count <= BLOCK_BUFFER_SIZE / block_shared.part[part].block_size &&
	    lba < block_shared.part[part].blocks && count <= block_shared.part[part].blocks - lba) {
		efi_block_io *bio = block_bios[part];

		bytes = count * block_shared.part[part].block_size;
		if (!hc_list_is_uefis()) {
			/* the kernel's own list is in: UEFI's driver must not ring now (the kernel should not ask) */
			if (hc_refused_reads++ < 4) {
				print("ufs: a read asked for while the kernel's own transfer list is in the controller: refused\n");
			}
			s = 0x800000000000000FULL;      /* EFI_ACCESS_DENIED */
			bytes = 0;
		} else if (op == BLOCK_OP_WRITE) {
			if (block_write_part < 0 || part != (uint32_t)block_write_part || lba < BLOCK_WRITE_FIRST) {
				/* not darwin, or not past what the build writes from the computer: never */
				if (block_writes_refused++ < 8) {
					print("ufs: a write of %d blocks from %d of %s: REFUSED\n", count, lba,
					    block_shared.part[part].name);
				}
				s = 0x800000000000000FULL;      /* EFI_ACCESS_DENIED */
				bytes = 0;
			} else {
				/*
				 * The kernel filled the buffer and cleaned its lines of it; drop whatever this
				 * core holds, write, and read back -- the answer says the blocks hold what the
				 * kernel gave, or the write is no good. The read back overwrites the buffer,
				 * which the kernel does not need: it has the data.
				 */
				uint32_t want;

				flush_dcache((uint64_t)(uintptr_t)block_buffer,
				    (uint64_t)(uintptr_t)block_buffer + bytes);
				want = ufs_crc32(block_buffer, bytes);
				s = bio->write_blocks(bio, bio->media->media_id, lba, bytes, block_buffer);
				if (!EFI_ERROR(s)) {
					bio->flush_blocks(bio);
					s = bio->read_blocks(bio, bio->media->media_id, lba, bytes, block_buffer);
					flush_dcache((uint64_t)(uintptr_t)block_buffer,
					    (uint64_t)(uintptr_t)block_buffer + bytes);
					if (!EFI_ERROR(s) && ufs_crc32(block_buffer, bytes) != want) {
						print("ufs: %d blocks from %d of darwin read back DIFFERENT\n",
						    count, lba);
						s = 0x800000000000000AULL;      /* EFI_VOLUME_CORRUPTED */
					}
				}
				if (!EFI_ERROR(s)) {
					block_writes++;
					block_write_bytes += bytes;
				}
			}
		} else if (op == BLOCK_OP_READ) {
			s = bio->read_blocks(bio, bio->media->media_id, lba, bytes, block_buffer);
			/* what the controller wrote is in memory; drop whatever this core held of the buffer */
			flush_dcache((uint64_t)(uintptr_t)block_buffer, (uint64_t)(uintptr_t)block_buffer + bytes);
		} else {
			bytes = 0;              /* an operation this loader does not know: nothing is touched */
		}
	}
	block_shared.answer.status = (int64_t)s;
	block_shared.answer.ticks += READ_SYSREG(CNTVCT_EL0) - started;
	if (EFI_ERROR(s)) {
		block_shared.answer.failed++;
	} else {
		block_shared.answer.served++;
		block_shared.answer.bytes += bytes;
	}
	__asm__ volatile ("dmb sy");
	block_shared.answer.answered = request;
	clean_dcache((uint64_t)(uintptr_t)&block_shared.answer,
	    (uint64_t)(uintptr_t)&block_shared.answer + sizeof(block_shared.answer));
}

static void
charger_start(void)
{
	static const efi_guid charger_ex_guid = QCOM_CHARGER_EX_GUID;
	static const efi_guid qcom_charger_guid = QCOM_CHARGER_GUID;
	efi_status ex_status, qcom_status, init_status = 0;

	if (charger_enabled || PLAT != &platform_redwood) {
		return;
	}
	ex_status = BS->locate_protocol(&charger_ex_guid, NULL, (void **)&CHARGER_EX);
	qcom_status = BS->locate_protocol(&qcom_charger_guid, NULL, (void **)&QCOM_CHARGER);
	if (!EFI_ERROR(qcom_status) && QCOM_CHARGER != NULL) {
		/* Re-open the driver's status path after QcomChargerApp handed off to the OS. */
		init_status = QCOM_CHARGER->initialize(1);
	}
	charger_enabled = (!EFI_ERROR(ex_status) && CHARGER_EX != NULL) ||
	    (!EFI_ERROR(qcom_status) && QCOM_CHARGER != NULL);
	print("battery: ChargerEx=%x QcomCharger=%x init=%x shared=%x\n", ex_status, qcom_status, init_status,
	    (uint64_t)(uintptr_t)&charger_shared);
	charger_poll();
	print("battery: flags=%x SOC=%d%% V=%d mV I=%d mA T=%d C action=%d\n", (uint64_t)charger_shared.flags,
	    (uint64_t)(int64_t)charger_shared.soc_percent, (uint64_t)(int64_t)charger_shared.voltage_mv,
	    (uint64_t)(int64_t)charger_shared.current_ma, (uint64_t)(int64_t)charger_shared.temperature_c,
	    (uint64_t)charger_shared.charger_action);
}

/* For the record: how much the kernel on the other core has logged so far, and the end of it. */
static void
note_kernel_progress(const struct kernel_live *live, uint64_t slide, uint64_t seconds)
{
	const char *log;
	uint32_t len, from;

	if (live == NULL) {
		print("%d s\n", seconds);
		return;
	}
	log = (const char *)(live->log + slide);
	/* its first words are written with the caches off: look at memory */
	flush_dcache(live->log_len + slide, live->log_len + slide + 4);
	len = *(volatile uint32_t *)(live->log_len + slide);
	if (len > live->log_head + live->log_ring) {
		len = 0;
	}
	from = len > 240 ? len - 240 : 0;
	flush_dcache((uint64_t)(uintptr_t)log + from, (uint64_t)(uintptr_t)log + len);
	print("%d s: the kernel has logged %d bytes, heartbeat %d; the last of it:\n", seconds, (uint64_t)len, live->heartbeat);
	for (uint32_t i = from; i < len; i++) {
		put_char(log[i] >= 0x20 && log[i] < 0x7f ? log[i] : log[i] == '\n' ? '\n' : '.');
	}
	print("\n");
}

/* A call of the firmware's power interface, the way this platform makes them. */
static int64_t
psci_call(uint64_t function, uint64_t a1, uint64_t a2, uint64_t a3)
{
	int64_t status;

	if (PLAT->psci_hvc) {
		register uint64_t x0 __asm__("x0") = function;
		register uint64_t x1 __asm__("x1") = a1;
		register uint64_t x2 __asm__("x2") = a2;
		register uint64_t x3 __asm__("x3") = a3;

		__asm__ volatile ("hvc #0" : "+r" (x0), "+r" (x1), "+r" (x2), "+r" (x3)
                        : : "x4", "x5", "x6", "x7", "x8", "x9", "x10", "x11", "x12", "x13", "x14", "x15", "x16", "x17");
		status = (int64_t)x0;
	} else {
		smc_x3 = a3;
		status = (int64_t)smc_call(function, a1, a2, NULL, NULL);
		smc_x3 = 0;
	}
	return status;
}

static uint64_t held_daif, held_group1;   /* interrupt state at a handoff, restored when the hold releases */
static int held_irq_masked;

/* LC-IRQ-BEGIN -- extracted verbatim by tools/test-loader-lifecycle.py: the one interrupt
 * mask that every path taking a hold performs before it touches anything else. The state
 * found at the first call is what the release point in LC-RETURN hands back; a later call
 * keeps that first record and only reasserts the mask. */
static void
held_irq_mask(void)
{
	uint64_t daif = READ_SYSREG(DAIF), group1 = READ_SYSREG(S3_0_C12_C12_7);

	if (!held_irq_masked) {
		held_daif = daif;
		held_group1 = group1;
		held_irq_masked = 1;
	}
	__asm__ volatile ("msr daifset, #0xf\nmsr S3_0_C12_C12_7, %0\nisb" : : "r" ((uint64_t)0));
}
/* LC-IRQ-END */

/* LC-WATCH-BEGIN -- extracted verbatim by tools/test-loader-lifecycle.py: one round of
 * watching the started core, exactly what run_on_other_cpu's loop runs per pass. It
 * returns nonzero when the loop must stop: the core returned (status stays 0) or a timeout
 * was decided (status set). A timeout ends the round right there: the interrupt mask comes
 * first, then nothing else of this round runs (no pet, no charger, no storage, no USB),
 * because from that moment the hold stands and the kernel may still own whatever these
 * services would touch. The note pass and the watch's first mask stay inline in
 * run_on_other_cpu (wiring-checked by the test). */
static int
lc_watch_round(const struct kernel_live *live, uint64_t slide, uint64_t started, uint64_t budget, int *arrived,
    uint64_t *last_pet, uint64_t *last_battery, uint64_t *last_beat, uint64_t *last_beat_at, int64_t *status)
{
	/* both are written by a core with its caches off: look at memory, not at our copy */
	flush_dcache((uint64_t)(uintptr_t)&other_cpu_done, (uint64_t)(uintptr_t)&other_cpu_done + 8);
	if (other_cpu_done) {
		/* what the kernel returned, written the same way */
		flush_dcache((uint64_t)(uintptr_t)&loader_ctx[CTX_LOG_PA], (uint64_t)(uintptr_t)&loader_ctx[CTX_KSP + 1]);
		return 1;
	}
	if (!*arrived) {
		flush_dcache((uint64_t)(uintptr_t)&other_cpu_stage, (uint64_t)(uintptr_t)&other_cpu_stage + 8);
		*arrived = other_cpu_stage != 0;
		if (!*arrived && seconds_since(started) >= START_SECONDS) {
			held_irq_mask();   /* the hold starts at the decision: nothing on this path runs first */
			*status = OTHER_CPU_NOT_STARTED;
			return 1;   /* held at once: the charger and the storage are not touched this round */
		}
	}
	if (*arrived && live != NULL) {
		if (live->heartbeat != *last_beat) {
			*last_beat = live->heartbeat;
			*last_beat_at = READ_SYSREG(CNTVCT_EL0);
		} else if (seconds_since(*last_beat_at) >= SILENCE_SECONDS) {
			held_irq_mask();   /* before this round's watchdog call and before any service */
			log_of_hung_kernel(live, slide);  /* the hung kernel's snapshot is read-only: kept */
			*status = OTHER_CPU_HUNG;
			return 1;   /* held at once: no block, USB or charger service past the decision */
		}
	}
	/* Feed this core's watchdog through the requested budget and a 60-second return margin. */
	if (wdt_armed && seconds_since(*last_pet) >= 5 && seconds_since(started) < budget + 60) {
		wdt_pet();
		*last_pet = READ_SYSREG(CNTVCT_EL0);
	}
	/* Qualcomm's UEFI charger stack owns the PMIC/GLINK side; keep XNU's cache live. */
	if (charger_enabled && seconds_since(*last_battery) >= 5 && hc_list_is_uefis()) {
		/* (not while the kernel's own transfer list is in: Qualcomm's DXEs are not to touch the storage) */
		charger_poll();
		*last_battery = READ_SYSREG(CNTVCT_EL0);
	}
	if (block_enabled) {
		block_poll();
	}
	usb_poll_command();
	if (block_enabled) {
		/*
		 * The pause is spent looking at the kernel's mailbox: with one look a pass, a
		 * request waited 60-75 us on average before this core noticed it, more than a
		 * 4 KB ReadBlocks takes (27 us). Same length of pause as below.
		 */
		for (int k = 0; k < 64; k++) {
			block_poll();
			for (volatile int i = 0; i < 300; i++) {
			}
		}
	} else {
		for (volatile int i = 0; i < 20000; i++) {
		}
	}
	return 0;
}
/* LC-WATCH-END */

/* Run the kernel on mpidr: 0 = returned, negative = PSCI refused, OTHER_CPU_NOT_STARTED
 * or OTHER_CPU_HUNG = incomplete. loader_ctx retains the available log for a hung kernel.
 * live holds its shared status; budget is the requested run duration in seconds. */
static int64_t
run_on_other_cpu(uint64_t mpidr, uint64_t entry, uint64_t boot_args_pa, const struct kernel_live *live, uint64_t slide,
    uint64_t budget)
{
	uint64_t started, last_pet, last_battery, last_beat_at, last_beat = 0, noted = 0;
	int64_t status;
	int arrived = 0, watching_only = 0;

	running_live = live;
	running_slide = slide;
	other_cpu_start.entry = entry;
	other_cpu_start.boot_args = boot_args_pa;
	other_cpu_stage = 0;
	other_cpu_done = 0;
	psci_by_hvc = PLAT->psci_hvc;
	/* that core starts with its caches off: all it reads must be in memory, not in this core's cache */
	clean_dcache((uint64_t)(uintptr_t)&other_cpu_start, (uint64_t)(uintptr_t)&other_cpu_start + sizeof(other_cpu_start));
	clean_dcache((uint64_t)(uintptr_t)&other_cpu_stage, (uint64_t)(uintptr_t)&other_cpu_stage + 8);
	clean_dcache((uint64_t)(uintptr_t)&other_cpu_done, (uint64_t)(uintptr_t)&other_cpu_done + 8);
	clean_dcache((uint64_t)(uintptr_t)&psci_by_hvc, (uint64_t)(uintptr_t)&psci_by_hvc + 8);
	clean_dcache((uint64_t)(uintptr_t)&other_cpu_entry, (uint64_t)(uintptr_t)&other_cpu_entry + 0x1000);
	clean_dcache((uint64_t)(uintptr_t)loader_ctx, (uint64_t)(uintptr_t)loader_ctx + sizeof(loader_ctx));
	/* annibale uses polled USB; keep firmware IRQ handlers out of XNU's GIC setup. The mask
	 * records the state the release point hands back, and a refused request restores it below. */
	if (PLAT == &platform_annibale) {
		watching_only = 1;
		held_irq_mask();
	}

	/* the last argument arrives in that core's x0 */
	/* LC-ENTER-BEGIN -- extracted verbatim by tools/test-loader-lifecycle.py: the bridge from
	 * the request to the watch, where the service exemption opens only on the answer 0
	 * (rev1 review section 3); LC-EXC closes it again at the fault entry. */
	status = lc_cpu_on_issue(mpidr, (uint64_t)(uintptr_t)&other_cpu_entry, (uint64_t)(uintptr_t)&other_cpu_start);

	started = last_pet = last_battery = last_beat_at = READ_SYSREG(CNTVCT_EL0);
	print("core %x: the firmware answers %d to the request to start it\n", mpidr, (uint64_t)status);
	if (status == 0) {
		/* the exemption covers the formal watch this answer opens and nothing else: a refused
		 * or unknown start keeps the held gates closed, its log stays in the RAM echo */
		active_watch = 1;
	} else if (kernel_resources_held) {
		/* an answer outside the refusal whitelist leaves the core's state unknown, so this
		 * path counts as held: mask before the flush below and before the caller's writes */
		held_irq_mask();
	}
	if (PLAT->log_other_cpu) {
		flush_log();
	}
	/* LC-ENTER-END */
	while (status == 0) {
		/* Storage logging needs firmware interrupts during NOTE_SECONDS on other platforms. */
		if (!watching_only && seconds_since(started) >= noted + 2) {
			noted = seconds_since(started);
			if (PLAT->log_other_cpu) {
				if (!arrived) {
					print("%d s: the core has not arrived\n", noted);
				} else {
					note_kernel_progress(live, slide, noted);
				}
				flush_log();
			}
		}
		/* Stop firmware interrupts before further use of XNU's shared interrupt controller. */
		if (!watching_only && seconds_since(started) >= NOTE_SECONDS) {
			watching_only = 1;
			held_irq_mask();
		}
		if (lc_watch_round(live, slide, started, budget, &arrived, &last_pet, &last_battery, &last_beat,
		    &last_beat_at, &status) != 0) {
			break;  /* returned, or a timeout decided at once: the hold stands, no service after it */
		}
	}
	/* LC-TAIL-BEGIN -- extracted verbatim by tools/test-loader-lifecycle.py: the loop tail
	 * after a round decided the hold, where the mask lands before the caller writes. */
	active_watch = 0;
	running_live = NULL;   /* the log pointer drops here; the hold does not until the proof */

	if (kernel_resources_held || firmware_fault) {
		/* interrupts stay off while the kernel may own the interrupt controller; an early
		 * exit that decided a timeout already masked, and one that did not gets the mask here,
		 * before the caller writes, pets or services anything. The release point in the
		 * LC-RETURN block hands the recorded state back. */
		held_irq_mask();
	} else if (held_irq_masked) {
		/* a refused request started nothing: hand the state back */
		__asm__ volatile ("msr S3_0_C12_C12_7, %0\nisb" : : "r" (held_group1));
		__asm__ volatile ("msr daif, %0" : : "r" (held_daif));
		held_irq_masked = 0;
	}
	return status;
	/* LC-TAIL-END */
}

