#!/usr/bin/env python3
"""Step the mount with short joystick pulses and grab a preview frame after
each, looking for structure (a roofline, a window) in a featureless field.
Every exit path sends scope_move ["none"] and restores the slew rate."""
import os, argparse, sys, time, json
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/Users/madsdorup/ASICAP/air-control")
from daypipes import host, Pipes, log, metrics, save_png, superpix, boxmean
from air_rpc import Air

ap = argparse.ArgumentParser()
ap.add_argument("--dir", required=True, choices=["north", "south", "east", "west"])
ap.add_argument("--steps", type=int, default=6)
ap.add_argument("--pulse", type=float, default=3.0)
ap.add_argument("--rate", type=int, default=4, help="slew rate index (4 = 20x ~5.3'/s)")
ap.add_argument("--exp", type=float, default=0.001)
ap.add_argument("--gain", type=int, default=0)
ap.add_argument("--bin", type=int, default=2)
ap.add_argument("--stop-at", type=float, default=8.0, help="stop when large-scale contrast %% exceeds this")
ap.add_argument("--tag", default="scan")
a = ap.parse_args()

class Joy:
    def __init__(self, host=None):
        host = host or globals()["host"]()
        self.host = host; self.m = Air(host, 4400, timeout=10)
    def c(self, meth, p=None):
        for i in range(3):
            try:
                return self.m.call(meth, p or [], timeout=12)
            except Exception:
                try: self.m.close()
                except Exception: pass
                time.sleep(0.4); self.m = Air(self.host, 4400, timeout=10)
        raise RuntimeError(meth)
    def state(self):
        return self.c("scope_get_info")["result"]
    def stop(self):
        try: self.c("scope_move", ["none"])
        except Exception: pass

def structure(img):
    sp = superpix(img)
    sm = boxmean(sp[::4, ::4], 9)          # ~72 px scale at bin2
    mean = max(float(sm.mean()), 1.0)
    return 100.0 * float(sm.std()) / mean, 100.0 * float(sm.max() - sm.min()) / mean

joy = Joy()
st = joy.state()
saved_rate = st["slew_rate_index"]
log("mount: RA %.4f Dec %.4f  Alt %.3f Az %.3f  pier %s  rate idx %d  track %s" % (
    st["RA"], st["Dec"], st["Alt"], st["Az"], st["pier_side"], saved_rate, st["is_enable_track"]))
p = Pipes()
try:
    joy.c("scope_set_slew_rate", [a.rate])
    log("slew rate set to %d (reads back %s)" % (a.rate, joy.state()["slew_rate_index"]))
    p.setup("preview", a.exp, a.gain, a.bin)
    img, w, h, info = p.grab()
    s1, s2 = structure(img)
    log("step 0 (no move): mean %.0f  contrast std %.2f%% range %.2f%%" % (img.mean(), s1, s2))
    save_png(img, "frames/%s_%s_00.png" % (a.tag, a.dir), shrink=2)
    for i in range(1, a.steps + 1):
        before = joy.state()
        joy.c("scope_move", [a.dir])
        t0 = time.time()
        time.sleep(a.pulse)
        joy.c("scope_move", ["none"])
        dt = time.time() - t0
        time.sleep(0.6)
        after = joy.state()
        dra = (after["RA"] - before["RA"]) * 15 * 60
        ddec = (after["Dec"] - before["Dec"]) * 60
        dalt = (after["Alt"] - before["Alt"]) * 60
        daz = (after["Az"] - before["Az"]) * 60
        img, w, h, info = p.grab()
        s1, s2 = structure(img)
        fn = "frames/%s_%s_%02d.png" % (a.tag, a.dir, i)
        save_png(img, fn, shrink=2)
        log("step %d %s %.1fs: dRA %+.1f' dDec %+.1f' | dAlt %+.1f' dAz %+.1f' -> Alt %.2f Az %.2f | mean %.0f  contrast std %.2f%% range %.2f%%  fresh=%s  %s" % (
            i, a.dir, dt, dra, ddec, dalt, daz, after["Alt"], after["Az"], img.mean(), s1, s2, info["fresh"], fn))
        if s1 > a.stop_at:
            log("structure found -- stopping here")
            break
finally:
    joy.stop()
    try:
        joy.c("scope_set_slew_rate", [saved_rate])
        log("slew rate restored to %d" % saved_rate)
    except Exception as e:
        log("could not restore slew rate: %s" % e)
    st = joy.state()
    log("mount now: RA %.4f Dec %.4f  Alt %.3f Az %.3f  move_status %s" % (st["RA"], st["Dec"], st["Alt"], st["Az"], st["move_status"]))
    p.close()
    log("closed")
