#!/usr/bin/env python3
"""Plate scale from register RA gotos on the Moon -- DO NOT TRUST AT SMALL STEPS.

WARNING (2026-09-27): the mount's RA is quantized to 1 s of time (14.7" on the sky
at Dec 10). A "1'" RA goto is 4.07 s and lands as 4 or 5 s, so this read 0.171"/px
against 0.1869 from driftscale.py. Kept only as a record; use driftscale.py.

    ASIAIR_HOST=192.168.1.36 python3 -u gotoscale.py --step-arcmin 1.5 --repeats 3 --exp-ms 10 --gain 220

Focus-page bin-1 crops (averaged in threes), goto RA +step / back / -step / back on
the sky, phase-correlate the lunar texture against the centre position. An RA
move of s arcmin on the sky is s/cos(Dec) in RA; Dec moves are NOT used (they
under-deliver ~12% and run 23 deg off north on this rig, 2026-09-27).
Brightness per frame is printed so cloud can be seen.
"""
import argparse, math, os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daypipes import host, Pipes, log
from mount import Mount
from moonreg import bandpass, measure_shift

ap = argparse.ArgumentParser()
ap.add_argument("--step-arcmin", type=float, default=1.5)
ap.add_argument("--repeats", type=int, default=3)
ap.add_argument("--exp-ms", type=float, default=10.0); ap.add_argument("--gain", type=int, default=220)
ap.add_argument("--settle-s", type=float, default=1.5)
a = ap.parse_args()

def green(f): return 0.5 * (f[0::2, 1::2].astype(np.float32) + f[1::2, 0::2])
p = Pipes(); m = Mount(host())
res = []
try:
    s0 = m.state(); ra0, dec0 = s0["RA"], s0["Dec"]
    log("register RA %.4f Dec %+.4f alt %.2f track %s" % (ra0, dec0, s0["Alt"], s0["is_enable_track"]))
    p.setup("focus", a.exp_ms / 1000.0, a.gain, 1); p.grab()
    def shoot():
        fs = [green(p.grab()[0]) for _ in range(3)]
        lv = [float(np.percentile(f, 99)) for f in fs]
        return bandpass(np.mean(fs, 0), 1.5, 20), lv
    def go(off):
        m.goto(ra0 + off / 60 / 15 / math.cos(math.radians(dec0)), dec0, timeout=60); time.sleep(a.settle_s); p.grab()
    for r in range(a.repeats):
        for sgn in (+1, -1):
            go(0); A, la = shoot()
            go(sgn * a.step_arcmin); B, lb = shoot()
            dx, dy, q = measure_shift(A, B, prepped=True)
            d = 2 * math.hypot(dx, dy)
            sc = a.step_arcmin * 60 / d
            res.append(sc)
            log("  r%d %+.1f': shift (%+.1f, %+.1f) = %.1f px, q %.2f -> %.5f\"/px   p99 %s / %s" %
                (r, sgn * a.step_arcmin, 2 * dx, 2 * dy, d, q, sc, " ".join("%.0f" % v for v in la), " ".join("%.0f" % v for v in lb)))
    go(0)
finally:
    m.close(); p.close()
res = np.array(res)
log("GOTO SCALE %.5f \"/px +- %.5f (sd %.2f%%, n %d) -> F %.0f mm" % (res.mean(), res.std() / math.sqrt(len(res)), 100 * res.std() / res.mean(), len(res), 598.17 / res.mean()))
