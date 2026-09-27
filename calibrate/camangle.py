#!/usr/bin/env python3
"""Camera angle on the sky in ~20 s: two full-sensor frames of the Moon with tracking paused
--pause-s between them. With tracking off the image slides sky-WEST (~136 px/s at 0.110"/px),
so the slide gives east on the sensor -- what moonadc.py --east needs. Re-run it whenever the
ADC or the camera has been turned (re-levelling the ADC turns the camera with it).
Tracking is always re-enabled, on Lunar.

    ASIAIR_HOST=192.168.1.36 python3 -u calibrate/camangle.py --pause-s 1.5
"""
import argparse, math, os, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from moonreg import bandpass, measure_shift

ap = argparse.ArgumentParser()
ap.add_argument("--pause-s", type=float, default=1.5)
ap.add_argument("--exp-ms", type=float, default=100.0); ap.add_argument("--gain", type=int, default=100)
ap.add_argument("--east-before", default="0.309,0.951", help="previous east, for the 'turned by' line (dustcheck 20:41)")
a = ap.parse_args()

def log(s): print("%s  %s" % (time.strftime("%H:%M:%S"), s), flush=True)
def res(r): return r.get("result", r) if isinstance(r, dict) else r
def mount(host, fn):
    from air_rpc import Air
    m = Air(host, 4400)
    try: return fn(m)
    finally: m.close()

def prep(img):
    g = (img[0::2, 1::2].astype(np.float64) + img[1::2, 0::2]) / 2
    disc = ndimage.gaussian_filter(g, 30) > 0.35 * np.percentile(g, 99)
    wt = ndimage.gaussian_filter(ndimage.binary_dilation(disc, iterations=30).astype(np.float64), 10)
    return bandpass(g, 2, 30) * wt, float(disc.mean())

from daypipes import Pipes, host
h = host(); p = Pipes(h); frames = []
try:
    p.setup("preview", a.exp_ms / 1000.0, a.gain, 1); p.grab(timeout=40)
    img, *_ = p.grab(timeout=40); frames.append(img); log("frame 0 (p99.5 %.0f)" % np.percentile(img, 99.5))
    t0 = time.time()
    mount(h, lambda m: m.call("scope_set_track_state", [False])); time.sleep(a.pause_s)
    mount(h, lambda m: m.call("scope_set_track_state", [True]))
    log("tracking paused %.2f s -> back on" % (time.time() - t0)); time.sleep(0.5)
    img, *_ = p.grab(timeout=40); frames.append(img); log("frame 1 (p99.5 %.0f)" % np.percentile(img, 99.5))
finally:
    try: p.close()
    except Exception: pass
    def restore(m):
        m.call("scope_set_track_state", [True])
        mo = res(m.call("scope_get_track_mode", []))
        if mo["list"][mo["index"]] != "Lunar": m.call("scope_set_track_mode", ["Lunar"])
        mo = res(m.call("scope_get_track_mode", []))
        return res(m.call("scope_get_track_state", [])), mo["list"][mo["index"]]
    try: log("RESTORED: tracking %s, mode %s" % mount(h, restore))
    except Exception as e: log("!! could not confirm tracking is back on (%s) -- CHECK THE MOUNT" % e)
(P0, d0), (P1, d1) = prep(frames[0]), prep(frames[1])
dx, dy, q = measure_shift(P0, P1, prepped=True)
v = np.array([2 * dx, 2 * dy]); east = -v / np.linalg.norm(v)
e0 = np.array([float(t) for t in a.east_before.split(",")]); e0 /= np.linalg.norm(e0)
turn = math.degrees(math.atan2(e0[0] * east[1] - e0[1] * east[0], float(e0 @ east)))
print("slide %+.0f %+.0f sensor px (q %.2f, disc %.0f%%/%.0f%%)" % (v[0], v[1], q, 100 * d0, 100 * d1))
print("EAST on the sensor: %+.3f,%+.3f   (camera turned %+.1f deg since --east-before)" % (east[0], east[1], turn))
