#!/usr/bin/env python3
"""tap.py X Y (pixels of the 1125x2436 framebuffer): a one-finger tap on QEMU's virtio multitouch, over QMP (env QMP, default 4455)."""
import json, os, socket, sys, time
x, y = int(sys.argv[1]), int(sys.argv[2])
s = socket.create_connection(("127.0.0.1", int(os.environ.get("QMP", "4455")))); f = s.makefile("rw")
f.readline(); f.write(json.dumps({"execute": "qmp_capabilities"}) + "\n"); f.flush(); f.readline()
def send(events):
    f.write(json.dumps({"execute": "input-send-event", "arguments": {"events": events}}) + "\n"); f.flush(); print(f.readline().strip())
X, Y = x * 32767 // 1125, y * 32767 // 2436
mtt = lambda t, axis="x", v=0: {"type": "mtt", "data": {"type": t, "slot": 0, "tracking-id": -1 if t == "end" else 7, "axis": axis, "value": v}}
btn = lambda d: {"type": "btn", "data": {"down": d, "button": "touch"}}
send([mtt("begin"), btn(True), mtt("data", "x", X), mtt("data", "y", Y)])
time.sleep(0.12)
send([mtt("end"), btn(False)])
