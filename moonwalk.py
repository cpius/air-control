#!/usr/bin/env python3
"""Walk the pointing in a straight line (register RA/Dec steps) with one frame
per step, printing the scattered-light level -- for "the Moon is up and to the
right of the scope, not far": no guesswork about the size of the offset.

    ASIAIR_HOST=192.168.1.36 python3 -u moonwalk.py --ddec 0.5 --dra -0.55 --steps 10

--dra is ON-SKY degrees per step (negative = west), --ddec degrees per step.
Stops at --steps, when the level exceeds --stop-factor x the first frame, or
when a frame is mostly lunar surface (level > --moon-adu).
"""
import argparse, math, os, sys, time
import numpy as np
sys.argv_saved = list(sys.argv)
ap = argparse.ArgumentParser()
ap.add_argument("--ddec", type=float, required=True); ap.add_argument("--dra", type=float, required=True)
ap.add_argument("--steps", type=int, default=10)
ap.add_argument("--exp", type=float, default=0.5); ap.add_argument("--gain", type=int, default=200)
ap.add_argument("--stop-factor", type=float, default=20.0)
ap.add_argument("--moon-adu", type=float, default=3000.0)
ap.add_argument("--min-alt", type=float, default=8.0)
w = ap.parse_args()
# reuse moonhunt's move/shoot machinery with its own defaults
sys.argv = [sys.argv[0], "--exp", str(w.exp), "--gain", str(w.gain), "--min-alt", str(w.min_alt), "--max-probes", "0"]
import importlib.util
here_dir = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(here_dir, "moonhunt.py")).read().split("\ntry:\n")[0]   # defs only, no main loop
g = {"__file__": os.path.join(here_dir, "moonhunt.py"), "__name__": "moonhunt_defs"}
exec(compile(src, "moonhunt.py", "exec"), g)
p, j, move_to, shoot, cmean, st, log = g["p"], g["j"], g["move_to"], g["shoot"], g["cmean"], g["st"], g["log"]

try:
    p.setup("preview", 0.001, w.gain, 2)
    img, *_ = p.grab(timeout=40); g["BIAS"] = cmean(img)
    log("bias %.1f" % g["BIAS"])
    p.setup("preview", w.exp, w.gain, 2)
    s = st(); ra0, dec0 = s["RA"], s["Dec"]
    first = shoot("walk0")
    for k in range(1, w.steps + 1):
        dec = dec0 + k * w.ddec
        ra = ra0 + k * w.dra / 15.0 / math.cos(math.radians(dec))
        s = move_to((ra, dec))
        log("step %d: register RA %.4f Dec %+.3f Alt %.2f Az %.2f" % (k, s["RA"], s["Dec"], s["Alt"], s["Az"]))
        lvl = shoot("walk%d" % k)
        if lvl > w.moon_adu:
            log("ON THE MOON (level %.0f)" % lvl); break
        if lvl > w.stop_factor * max(first, 1.0):
            log("level %.0f is %.0fx the start -- close; stopping for a look" % (lvl, lvl / max(first, 1))); break
    s = st(); log("END RA %.4f Dec %+.3f Alt %.2f Az %.2f" % (s["RA"], s["Dec"], s["Alt"], s["Az"]))
finally:
    j.close()
