# Camera under AVF: SCCAM, the phone's camera in iOS

With the Scion app's per-install **Camera** setting, iOS's Camera.app (and everything else on
AVFoundation) gets the frames of the camera of the phone the VM runs on. The app captures with
Camera2 and lends the VM a second slice of shared RAM, as it does for the display (SCFB,
[DISPLAY.md](DISPLAY.md)). The camera needs the AVF display: both regions go through the same
`createVm` hook.

```
Pixel camera --Camera2--> app (ImageReader, YUV_420_888)
        --NV12 into a ring of slots--> SCCAM region (ashmem, guest RAM)
        --> XNU /dev/redwood-camera (patch 1400): RAW10, every pixel a 2x2 RGGB cell
        --> libredwood-media.dylib in mediaserverd (camera_device.c, camera_isp.c)
        --> CMCapture's graph --> Camera.app (preview, photos)
```

Above the kernel nothing changes: the media library, its ISP and the capture graph run as on
HackiOS's phone, with `/dev/redwood-camera` as the interface. The kernel makes each NV12 frame into
the RAW10 frame a sensor would give, every pixel of the app's one cell of four sites, so the ISP's
preview path (a cell a pixel) gets the app's pixels back; the colours are linear light, so the ISP's
gamma restores them (round trip within 4/255 of the app's pixels, measured on the host with the
conversion compiled stand-alone). The ISP's exposure loop
still runs: the kernel makes the frames as bright as the exposure asked for (the first exposure gives
the app's levels), so AE closes on its own target. There is no colour tuning on the AVF image
(no `fw/camera-*.isp`): the ISP's colour matrix is the identity.

## Plumbing

* **The app** adds to the kernel parameters `sccam.addr=<IPA> sccam.size=<bytes>`, optionally
  `sccam.w=W sccam.h=H` (the pictures' size; default 1280x960). The region (16 MiB) sits directly below
  the SCFB region. The app writes the header before boot.
* **avfboot** reads `sccam.addr`/`sccam.size`, ends XNU's memory window below the region, and adds the
  device-tree nodes MobileGestalt looks for (`/arm-io/isp` with `camera-rear`, `/product/camera`), as
  xnuloader does for `cam=5`. That puts Camera.app on the home screen.
* **Patch 1400** (`bsd/dev/redwood_camera.c`): with `sccam.addr`, `/dev/redwood-camera` serves the
  app's frames (no `cam=` needed). It maps the region cacheable (the app's mapping is too), checks
  the header, and on every session (a read wants frames) asks for them. Sensor 1 (camera_device.c's
  back camera) is the phone's back camera, sensor 2 its front one. `sccam.turn=MASK` turns the back
  (1) or front (2) camera's frames half round.

## The region

Little-endian. `H` the app writes, `G` the guest.

| Offset | | Field | |
|---|---|---|---|
| 0x00 | u32 | magic | `SCCM` (0x4d434353), H, before boot |
| 0x04 | u32 | version | 1 |
| 0x08 | u32 | header_bytes | where the slots begin (64 KiB) |
| 0x0c | u32 | nslots | 2..4 |
| 0x10 | u32 | slot_bytes | room for one frame |
| 0x14 | u32 | host_flags | bit 0 READY (the app is attached and may use the camera), bit 1 DENIED (no CAMERA permission), bit 2 NO_CAMERA |
| 0x18 | u32 | host_state | 0 idle, 1 opening, 2 streaming, 3 error |
| 0x1c | u32 | host_error | Camera2's error code, when state is 3 |
| 0x20 | u64 | host_ns | the app's heartbeat (CLOCK_MONOTONIC), every 100 ms or so |
| 0x28 | u32 | req_ack | the req_seq the app has acted on |
| 0x2c | u32 | cameras | bit 0 the phone has a back camera, bit 1 a front one |
| 0x30 | u64 | frame_seq | the last frame published (0: none yet) |
| 0x38 | u32 | latest | its slot |
| 0x40 | u32 | req_seq | G: one more on every change of the fields below |
| 0x44 | u32 | req_on | G: 1, frames wanted |
| 0x48 | u32 | req_facing | G: 0 back, 1 front |
| 0x4c | u32 | req_width | G: the size wanted; the app takes the nearest it can, the guest crops and scales |
| 0x50 | u32 | req_height | |
| 0x54 | u32 | req_fps | G: 30 |
| 0x58 | u32 | guest_reading | G: 1 + the slot being read, 0 none: the app writes another |
| 0x60 | u64 | guest_ns | G: the guest's heartbeat while frames are wanted; the app stops the camera when it is 3 s old |
| 0x100 + 64 i | | slot i | |

A slot's descriptor (64 bytes):

| Offset | | Field | |
|---|---|---|---|
| 0x00 | u64 | seq | odd while the app writes the slot, else 2 x the frame's number |
| 0x08 | u64 | time_ns | the sensor's timestamp |
| 0x10 | u32 | offset | the frame, from the region's start (64 KiB aligned) |
| 0x14 | u32 | bytes | |
| 0x18 | u32 | width, height | even |
| 0x20 | u32 | format | `NV12` (0x3231564e) or `NV21` (0x3132564e), full range BT.601 (Camera2's JFIF YUV) |
| 0x24 | u32 | stride_y, stride_uv | |
| 0x2c | u32 | uv_offset | the chroma plane, from the frame's start |
| 0x30 | u32 | facing | 0 back, 1 front |
| 0x34 | u32 | orientation | the sensor's (Camera2's SENSOR_ORIENTATION), for information |

**Publishing** (app): pick a slot that is neither `latest` nor `guest_reading - 1`; `seq` = odd;
release fence; the pixels and the descriptor; release fence; `seq` = 2 n; `latest` = the slot;
release fence; `frame_seq` = n.

**Reading** (guest): `frame_seq`, then `latest`; the slot's `seq` (even, non-zero); set
`guest_reading`; copy; acquire fence; `seq` again: if it changed the frame is torn and dropped.

The control path is the header itself: the app polls `req_seq` (every 50 ms), the guest polls
`frame_seq` (every 4 ms while a session runs). No vsock, no interrupts.

## QEMU

`avf/qemu-scfb.sh` with `CAM=1` backs guest RAM with a shared file and runs `avf/sccam-host.py`
on it, which plays the app: the header before boot, then a test pattern (colour bars, a moving square,
the back and front cameras told apart) whenever the guest asks.
