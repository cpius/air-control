#!/usr/bin/env python3
"""End-to-end proof: drive the ASI585MC Air over Alpaca and pull real pixels.

Uses a small subframe so imagearray comes back as JSON in a sane amount of
time — the full 3840x2160 frame is 8.3M values and is painful over JSON.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from airlog import get_logger
from alpaca import Alpaca

log = get_logger("smoke")

HOST = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ASIAIR_HOST")
if not HOST:
    sys.exit("usage: python3 smoke_test.py <host> [port]   (or set ASIAIR_HOST)")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 32323
CAM = 1  # 0 = ASI220MM guide sensor, 1 = ASI585MC main sensor

c = Alpaca(HOST, PORT)
print("connected:", c.connect(CAM))

w = c.get("camera", CAM, "cameraxsize")
h = c.get("camera", CAM, "cameraysize")
print(f"sensor {w}x{h}")

# 256x256 subframe from the centre
side = 256
c.put("camera", CAM, "binx", BinX=1)
c.put("camera", CAM, "biny", BinY=1)
c.put("camera", CAM, "startx", StartX=(w - side) // 2)
c.put("camera", CAM, "starty", StartY=(h - side) // 2)
c.put("camera", CAM, "numx", NumX=side)
c.put("camera", CAM, "numy", NumY=side)
c.put("camera", CAM, "gain", Gain=252)
print("gain now:", c.get("camera", CAM, "gain"))

for secs in (0.01, 0.5):
    t0 = time.time()
    c.put("camera", CAM, "startexposure", Duration=secs, Light=True)
    # Even a 10 ms exposure can sit here for seconds waiting on readout.
    with log.slow("exposing %.2fs" % secs, quiet_for=1.0,
                  detail=lambda: "%.1fs waiting on imageready" % (time.time() - t0)):
        while not c.get("camera", CAM, "imageready"):
            time.sleep(0.1)
    img = c.get("camera", CAM, "imagearray")
    flat = [v for row in img for v in row]
    log.info("%.2fs frame: %dx%d in %.1fs", secs, len(img), len(img[0]),
             time.time() - t0)
    print(f"  {secs:>5}s -> {len(img)}x{len(img[0])}  "
          f"min={min(flat)} max={max(flat)} mean={sum(flat)/len(flat):.1f}  "
          f"({time.time()-t0:.1f}s)")

print("camera state:", c.get("camera", CAM, "camerastate"))
print("temp:", c.get("camera", CAM, "ccdtemperature"))
