#!/usr/bin/env python3
"""Fast frames on the preview page, without waiting for its plate solve.

Measured on this rig, 2026-09-10:
  * `focus` and `rtmp` raise Exposure "start" and never complete. Only
    `preview` completes an exposure (3.9 s at 1 s / bin2).
  * `preview` auto-solves and annotates every frame. At gross defocus the
    solver finds ~950 spurious sources and grinds -- one solve was aborted
    after 106 s -- which blocks the next frame and stalls RPC calls.

The image is ready at the Exposure "complete" event, BEFORE the solve finishes.
So: start the exposure, wait for that event, download immediately, then
stop_solve to kill the annotate before it eats the next frame. ~6 s a frame
instead of 60-300 s.
"""
import argparse, sys, time
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from session import Session
from daypipes import host as air_host, KEY, log

class Grabber:
    def __init__(self, exp=1.0, gain=250, binning=2, host=None, key=KEY):
        self.exp = exp
        self.s = Session(host or air_host(), key, with_mount=False)
        self.s.c("stop_solve"); self.s.c("stop_exposure"); time.sleep(1.2)
        self.s.c("set_page", ["preview"]); time.sleep(1.0)
        self.s.c("set_camera_bin", [int(binning)])
        self.s.set_exp(exp, gain)
        self.bin = binning
        # Prime the content hash. Session.fresh() rejects a repeat by comparing
        # against the LAST frame it saw, and on a brand-new Session there is no
        # last frame -- so the very first grab happily accepts whatever stale
        # image the Air still had cached. That is not a slow frame or an empty
        # sky; it is the previous pointing, and it reads as neither.
        try:
            self.s.c("start_exposure")
            self.s.fresh(1)
        except Exception:
            pass

    def set_exp(self, exp, gain):
        self.exp = exp; self.s.set_exp(exp, gain)

    def frame(self, timeout=40.0):
        s = self.s
        s.c("stop_solve")                 # never inherit a grinding solve
        s.air.drain_events()
        s.c("start_exposure")
        t0 = time.time(); ready = False
        while time.time() - t0 < timeout:
            for e in s.air.drain_events():
                if e.get("Event") == "Exposure" and e.get("state") in ("complete", "downloading"):
                    ready = True
            if ready: break
            s.c("get_camera_state")
            time.sleep(0.3)
        if not ready:
            raise RuntimeError("exposure did not complete in %.0fs" % timeout)
        v, w, h = s.fresh(1)              # the frame is ready; one download is enough
        s.c("stop_solve")                 # kill the annotate before it blocks the next one
        raw = np.frombuffer(v, dtype=np.uint8)
        img = raw.reshape(h, w) if raw.size == w*h else raw.view(np.uint16).reshape(h, w)
        return img.astype(np.float32), w, h

    def close(self):
        try: self.s.c("stop_solve")
        except Exception: pass
        self.s.close()

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Time a few preview-page grabs.")
    ap.add_argument("--exp", type=float, default=1.0, help="exposure, s")
    ap.add_argument("--gain", type=int, default=250)
    ap.add_argument("--bin", type=int, default=2)
    ap.add_argument("--frames", type=int, default=4)
    a = ap.parse_args()
    g = Grabber(exp=a.exp, gain=a.gain, binning=a.bin)
    try:
        for i in range(a.frames):
            t = time.time()
            img, w, h = g.frame()
            log("frame %d: %dx%d in %.1fs  med=%.0f max=%.0f" % (i+1, w, h, time.time()-t,
                np.median(img), img.max()))
    finally:
        g.close(); log("closed")
