#!/usr/bin/env python3
"""One short joystick pulse, then a preview frame: where did the bright band go?
Reports the rows (sensor px) of the strongest bright->dark and dark->bright
horizontal edges in the column-averaged profile."""
import os, argparse, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from daypipes import host, Pipes, log, save_png, superpix
from air_rpc import Air

ap = argparse.ArgumentParser()
ap.add_argument("--dir", default=None, choices=[None, "north", "south", "east", "west"])
ap.add_argument("--pulse", type=float, default=0.5)
ap.add_argument("--rate", type=int, default=4)
ap.add_argument("--exp", type=float, default=0.0015)
ap.add_argument("--tag", default="nudge")
a = ap.parse_args()

def band_rows(img):
    sp = superpix(img); prof = sp[:, 200:760].mean(axis=1)
    prof = np.convolve(prof, np.ones(5) / 5, mode="same"); g = np.gradient(prof)
    g[:8] = 0; g[-8:] = 0
    top = int(np.argmax(g)); bot = int(np.argmin(g))       # dark->bright (band top), bright->dark (band bottom)
    return top * 4, bot * 4, float(prof.max()), float(prof.min())

m = Air(host(), 4400, timeout=10)
def mc(meth, p=None): return m.call(meth, p or [], timeout=12)["result"]
saved = mc("scope_get_info")["slew_rate_index"]
p = Pipes()
try:
    p.setup("preview", a.exp, 0, 2)
    img, w, h, info = p.grab()
    t, b, hi, lo = band_rows(img)
    log("before: band top at sensor row %d, bottom at %d (profile max %.0f min %.0f)" % (t, b, hi, lo))
    save_png(img, "frames/%s_before.png" % a.tag, shrink=2)
    if a.dir:
        mc("scope_set_slew_rate", [a.rate])
        s0 = mc("scope_get_info")
        mc("scope_move", [a.dir]); time.sleep(a.pulse); mc("scope_move", ["none"]); time.sleep(0.8)
        s1 = mc("scope_get_info")
        log("pulsed %s %.2fs at rate %d: dDec %+.1f' dRA %+.1f'" % (a.dir, a.pulse, a.rate, (s1["Dec"]-s0["Dec"])*60, (s1["RA"]-s0["RA"])*900))
        img, w, h, info = p.grab(); img, w, h, info = p.grab()
        t, b, hi, lo = band_rows(img)
        log("after:  band top at sensor row %d, bottom at %d (profile max %.0f min %.0f)" % (t, b, hi, lo))
        save_png(img, "frames/%s_after.png" % a.tag, shrink=2)
finally:
    try: mc("scope_move", ["none"]); mc("scope_set_slew_rate", [saved])
    except Exception as ex: log("mount restore failed: %s" % ex)
    p.close(); log("closed")
