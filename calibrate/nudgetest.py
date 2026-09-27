#!/usr/bin/env python3
"""Test slowpulse.Nudger on lunar surface: commanded pointing nudges vs the measured image shift.

Same measurement as joytest.py (focus page, band-passed 2x2-superpixel phase correlation, one frame
discarded after the move). The sequence reverses Dec on purpose, so the backlash compensation is
exercised; moves stay <= 30" E-W (the focus crop's short axis is 2.6').

    ASIAIR_HOST=192.168.1.36 python3 -u calibrate/nudgetest.py --east=-0.070,-0.998 --arcsec-per-px 0.1866
"""
import argparse, os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from daypipes import Pipes, host, log
from moonreg import bandpass, measure_shift
from slowpulse import Nudger

ap = argparse.ArgumentParser()
ap.add_argument("--east", required=True, help="sky east on the sensor 'x,y'")
ap.add_argument("--arcsec-per-px", type=float, required=True, help="bin-1 scale")
ap.add_argument("--exp-ms", type=float, default=10.0); ap.add_argument("--gain", type=int, default=220)
ap.add_argument("--moves", default="0:10,0:10,0:-10,0:-10,10:0,-10:0,0:6,0:-6,20:20,-20:-20,6:-6,-6:6,30:0,-30:0,0:40,0:-40,0:60,0:-60",
                help="east:north arcsec pairs")
ap.add_argument("--backlash", type=float, default=8.0)
ap.add_argument("--settle", type=float, default=0.8)
a = ap.parse_args()
E = np.array([float(v) for v in a.east.split(",")]); E /= np.linalg.norm(E); N = np.array([-E[1], E[0]])

p = Pipes()
def frame():
    img, *_ = p.grab(timeout=30); img = img.astype(np.float64)
    return bandpass(img[0::2, 0::2] + img[1::2, 1::2] + img[0::2, 1::2] + img[1::2, 0::2], 1.5, 20)
def pointing_shift(ref, mov):          # features move opposite to the pointing (joytest calibration)
    dx, dy, q = measure_shift(ref, mov, prepped=True)
    d = np.array([dx, dy]) * 2.0
    return -float(d @ E) * a.arcsec_per_px, -float(d @ N) * a.arcsec_per_px, q   # minus: the image moves opposite to the pointing

nud = Nudger(host(), backlash=a.backlash, log=log); errs = []
try:
    p.setup("focus", a.exp_ms / 1000.0, a.gain, 1); frame()
    for spec in a.moves.split(","):
        e_cmd, n_cmd = (float(v) for v in spec.split(":"))
        ref = frame(); t0 = time.time()
        done = nud.nudge(e_cmd, n_cmd)
        took = time.time() - t0
        time.sleep(a.settle); frame(); e, n, q = pointing_shift(ref, frame())
        errs.append((e_cmd, n_cmd, e, n, q))
        log("asked %+5.0f\" E %+5.0f\" N -> moved %+6.1f\" E %+6.1f\" N  (error %+5.1f %+5.1f)  q %.2f  %.1f s  [%s]" % (
            e_cmd, n_cmd, e, n, e - e_cmd, n - n_cmd, q, took, ", ".join("%s %.0f\" %dx" % (c, amt, 1 if r == 0 else 4) for c, amt, r, _ in done)))
finally:
    p.close()
ok = [(e - ec, n - nc) for ec, nc, e, n, q in errs if q > 0.5]
if ok:
    r = np.array(ok)
    print("\n%d good readings: error E median %+.1f\" rms %.1f\" ; N median %+.1f\" rms %.1f\"  (floor ~4\" in ~3\" seeing)" % (
        len(r), np.median(r[:, 0]), np.sqrt(np.mean(r[:, 0] ** 2)), np.median(r[:, 1]), np.sqrt(np.mean(r[:, 1] ** 2))))
