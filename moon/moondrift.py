#!/usr/bin/env python3
"""Drift of the lunar surface across the sensor from a timed frame series:
phase correlation of each frame against the first (green plane, high-passed),
linear fit in px/s, converted to arcsec/s and, through a Jacobian, to RA/Dec
arcmin/min. Works on the .npy files moonlook.py --every writes.

    python3 moondrift.py --json telemetry/2026-09-24/moonlook/HHMMSS_drift.json --arcsec-per-px 0.288 --jacobian=-23,299.8,-237.5,27.5
"""
import argparse, glob, json, math, os, sys
import numpy as np
from scipy import ndimage

ap = argparse.ArgumentParser()
ap.add_argument("--json", required=True, help="moonlook run record")
ap.add_argument("--arcsec-per-px", type=float, default=0.288, help="scale of the frames as taken (bin 2 -> 0.288)")
ap.add_argument("--jacobian", default=None, help="bin-2 px per arcmin, star shift per register move")
ap.add_argument("--lo", type=float, default=1.5, help="smoothing sigma, plane px"); ap.add_argument("--hi", type=float, default=25.0, help="high-pass sigma, plane px")
ap.add_argument("--no-static", action="store_true", help="do not subtract the per-pixel temporal median (the sensor's fixed pattern)")
a = ap.parse_args()
from moonreg import bandpass, measure_shift

rec = json.load(open(a.json)); rows = rec["rows"]
def green(img):
    return img[0::2, 1::2].astype(np.float64)
frames = [green(np.load(r["npy"])) for r in rows]
static = None if a.no_static or len(frames) < 4 else np.median(np.stack(frames), axis=0)
if static is not None:
    # a moving scene's temporal median is dominated by the static pattern; remove it, then remove the residual mean scene
    print("subtracting the per-pixel temporal median of %d frames (static pattern; sd %.0f ADU)" % (len(frames), float((static - ndimage.gaussian_filter(static, a.hi)).std())))
def prep(g):
    return bandpass(g - static if static is not None else g, a.lo, a.hi)
def shift(refp, movp):
    dx, dy, q = measure_shift(refp, movp, prepped=True)
    return dx * 2, dy * 2, q          # plane px -> frame px
preps = [prep(g) for g in frames]
T, X, Y = [], [], []
for k, r in enumerate(rows):
    dx, dy, pk = shift(preps[0], preps[k])
    step = shift(preps[k - 1], preps[k]) if k else (0.0, 0.0, 1.0)
    T.append(r["t_rel"]); X.append(dx); Y.append(dy)
    print("t=%6.1fs  shift vs first dx %+8.2f dy %+8.2f px (q %.3f) ; vs previous dx %+7.2f dy %+7.2f (q %.3f)" % (r["t_rel"], dx, dy, pk, step[0], step[1], step[2]))
T, X, Y = np.array(T), np.array(X), np.array(Y)
if len(T) < 2:
    sys.exit("need at least two frames")
vx, vy = np.polyfit(T, X, 1)[0], np.polyfit(T, Y, 1)[0]
rx = X - np.polyval(np.polyfit(T, X, 1), T); ry = Y - np.polyval(np.polyfit(T, Y, 1), T)
s = a.arcsec_per_px
print("DRIFT: %+.3f px/s x, %+.3f px/s y = %+.3f\"/s, %+.3f\"/s  |%.3f\"/s| = %.1f\"/min  over %.0f s (fit residual sd %.2f/%.2f px)" % (
    vx, vy, vx * s, vy * s, math.hypot(vx, vy) * s, math.hypot(vx, vy) * s * 60, T[-1], rx.std(), ry.std()))
w, h = rows[0]["w"], rows[0]["h"]
if abs(vx) > 1e-6 or abs(vy) > 1e-6:
    tx = (w / 2) / abs(vx) if vx else float("inf"); ty = (h / 2) / abs(vy) if vy else float("inf")
    print("time for the field centre to reach an edge at this rate: %.0f s" % min(tx, ty))
if a.jacobian:
    J = np.array([float(v) for v in a.jacobian.split(",")]).reshape(2, 2)
    # the image moves by v px/s; a register move m (arcmin) moves the image by J m. The sky drift equals a register error growing at -J^-1 v per s
    d = np.linalg.solve(J, np.array([vx, vy])) * 60          # arcmin/min of "register move" that would reproduce the image motion
    print("equivalent register motion: RA %+.2f'/min (on sky)  Dec %+.2f'/min  -> the Moon's image moves as if the register drifted by the negative of this" % (d[0], d[1]))
