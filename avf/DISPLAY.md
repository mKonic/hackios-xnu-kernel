# Display and touch under AVF (crosvm on the Pixel)

HackiOS needs a framebuffer: without `disp0`, SpringBoard doesn't start. On
QEMU that framebuffer is ramfb, set up by the loader. crosvm has no ramfb. Its
virtio-gpu display can't reach a sideloaded app on the Pixel 10 either: that
build's crosvm rejects `--android-display-service`, and SELinux reserves the
display service for the Terminal app. The Android side was measured on the
phone; see scion-avf `docs/DISPLAY-INPUT.md`.

So there are two display paths, and touch over virtio-input.

## 1. SCFB: the shared framebuffer (the AVF path)

The Scion app lends the VM a slice of RAM that it maps too: an ashmem region
that crosvm maps as guest memory (`--file-backed-mapping`). It is the AVF
counterpart of ramfb, with a small header (scion-avf `display/SCFB.md`). On the
Pixel the app presents a 1080×2424 buffer at 120 fps for 1.4 ms of copying per
frame.

* **The app** adds to the kernel parameters:
  `scfb.addr=<IPA> scfb.size=<bytes> scfb.w=1125 scfb.h=2436`. The region
  sits inside guest RAM, below the FDT at the top. The panel is 1125×2436, the
  logical size MiniIOMobileFramebuffer already uses, so the kernel doesn't
  scale. The app's SurfaceView scales to the phone's screen on the GPU.
* **avfboot** (`avf/shim/avfboot.c`):
  * reads those four parameters from `/chosen/bootargs`;
  * ends XNU's memory window below `scfb.addr`;
  * writes the header: XR24 (B,G,R,X, what iOS draws), one buffer at +64 KiB,
    `flags = READY|POLL`;
  * points `boot_args.video` at the buffer. From there XNU's text console and
    MiniIOMobileFramebuffer find it the usual way (`PE_state.video` → `vinfo`).
* **Patch 1200** (`xnu/patches/1200-avf-scfb-display.patch`,
  MiniIOMobileFramebuffer):
  * at `start()`, maps the header page (`scfb.addr`) and checks the magic;
  * after each `show()` has copied a frame into the panel's memory, it runs a
    `dsb`, then sets `dirty` and `guest_ns`, `frame_seq++`, and clears `POLL`.
    The app copies on `frame_seq` changes.
  * Until the first frame, `POLL` makes the app copy at the refresh rate, so
    the verbose boot console is visible too.

**No tearing.** There is one buffer, drawn in place, because a swap's layers
only redraw what they cover. It is guarded like a seqlock:

* Before drawing, the kernel waits while the app is copying (`host_reading`,
  4 ms at most) and sets `drawing`.
* The app waits while `drawing` is set, and copies again if a frame began
  meanwhile.

Measured on the Pixel with the test guest in this mode (scion-avf
`display/run-on-device.sh --bufs 1`):

| Mode | Frames presented | Torn |
|---|---|---|
| Seqlock, guest drawing back to back at 820 fps | 599 at 120 fps | 0 frames, 0 sampled frames mixing rows |
| Same guest without the seqlock (`--bufs 0`, like ramfb) | 345 | 128 mixed rows from two frames |

A flipped pair (`nbufs = 2`) also works in the app, but it would need
MiniIOMobileFramebuffer to recompose the whole panel each frame.

**No-copy:** the guest→host direction has no copy: the frame lands in memory
the app has mapped. The one remaining copy is the app's `memcpy` into its
buffer queue.

## 2. virtio-gpu 2D (QEMU, or a crosvm that shows its display)

**Patch 1202** (`bsd/dev/redwood_vgpu.c`, `bsd/sys/redwood_vgpu.h`) runs on the
virtio core from PR #1 (`sys/redwood_virtio.h`). Without the core it builds as
stubs that return `ENODEV`.

Setup:

* `vio_find(VIRTIO_ID_GPU, 0)`, then `vio_init`. When offered, it negotiates
  `VIRTIO_GPU_F_VIRGL` and `VIRTIO_GPU_F_CONTEXT_INIT`, for the 3D work. This
  doesn't change 2D.
* Control queue 0. One command at a time under a mutex, each one waited for.
* `GET_DISPLAY_INFO` gives the size (boot-args `vgpu_width`/`vgpu_height`
  override it; the default is 1125×2436).
* Resource 1 is `RESOURCE_CREATE_2D` in `B8G8R8X8_UNORM`. Its backing is one
  contiguous `vio_dma_alloc` buffer (`RESOURCE_ATTACH_BACKING`, one entry),
  and it goes on scanout 0 (`SET_SCANOUT`).

MiniIOMobileFramebuffer uses it only when the loader left no framebuffer:

* `start()` takes the scanout's memory as the panel;
* `show()` ends each frame with `redwood_vgpu_flush()`, which sends
  `TRANSFER_TO_HOST_2D` and then `RESOURCE_FLUSH`.

**For 3D on top** (branch `avf/gpu3d`, `redwood_vgpu3d.c`):

* `int redwood_vgpu_ctrl(const void *cmd, uint32_t cmd_len, void *resp, uint32_t resp_len)`
  puts one command on the control queue, serialized with 2D. It copies through
  64 KiB / 4 KiB device-visible buffers and may sleep.
* `uint64_t redwood_vgpu_features(void)` returns the negotiated `VIRTIO_GPU_F_*` bits.
* Resource IDs below `REDWOOD_VGPU_3D_FIRST_ID` (0x10000) are 2D's; 3D
  allocates from there up.
* Fences, `RESOURCE_BLOB` and `SET_SCANOUT_BLOB` come later. A blob scanout on
  guest memory would remove the per-frame transfer, the virtio-gpu version of
  SCFB's no-copy path.

## 3. Touch: virtio-input multi-touch

**Patch 1201** (`bsd/dev/redwood_virtio_input.c`). With the virtio core, it:

* finds every input device on either transport (crosvm puts them on PCI);
* reads the device's name and axis ranges (`ABS_INFO` for `ABS_X/Y` and
  `ABS_MT_POSITION_X/Y`) from configuration space, instead of assuming QEMU's
  0..0x7fff;
* tracks `ABS_MT_SLOT`/`ABS_MT_TRACKING_ID`. Each contact (up to 10) is its own
  finger, and every `EV_SYN` posts DOWN, MOVE or UP to `/dev/redwood-touch`;
* runs from `vio_service_start` (interrupt or 8 ms poll), in thread context,
  as `redwood_touch_post` requires.

The userland side (inject.c in backboardd) already turns those records into
IOHID digitizer events with up to 10 fingers. Single-touch pointers (QEMU's
tablet) and the keyboard (buttons, F11/F12) behave as before. Without the
virtio core, the old virtio-mmio code is built, with the multi-touch handling
added.

The Scion app creates the device: `InputDevice.MultiTouch` with range
0..width/height of the framebuffer, and it writes the same evdev stream the
Android framework would. On the Pixel all 400 scripted reports reached a Linux
guest's evdev with exact coordinates, and real touches reached the posted frame
at p50 10.6 ms.

## 4. Status and what's open

* Built by CI (`xnu-linux.yml`). Not yet booted under crosvm: that needs the
  virtio core (PR #1) and avfboot (PR #2). The SCFB path doesn't depend on the
  virtio core.
* The vio-based input and GPU code compile only once
  `bsd/sys/redwood_virtio.h` is in the tree (`__has_include`). PR #1's header
  is what they're written against.
* The app side for iOS: pass the `scfb.*` parameters and present XR24 (the
  BGRA buffer queue gives a plain `memcpy`). scion-avf `display/app` shows how.
