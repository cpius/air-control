#!/usr/bin/env python3
"""Where is the Moon's centre relative to the OTA field? Fit a fixed-radius
circle to the limb in one preview frame and report the disc centre in pixels
and, through the camera Jacobian, the register move that would centre it.

    ASIAIR_HOST=192.168.1.36 python3 -u limbfit.py --diam-arcmin 31.8 --jacobian=-23,299.8,-237.5,27.5

The 9'x5' barlow field sees only a short arc, so the radius cannot be fitted:
it is fixed from the ephemeris diameter and only the centre (2 parameters) is
solved -- the arc's normal fixes the direction, the known radius the distance.
Jacobian: bin-2 px per arcmin, [[dx/dRA, dx/dDec], [dy/dRA, dy/dDec]] (star
shift per register move; pier west 2026-09-16 = the negated pier-east one).
"""
import argparse, math, os, sys, time
import numpy as np
from scipy import ndimage, optimize
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daypipes import host, Pipes, log, save_png

ap = argparse.ArgumentParser()
ap.add_argument("--exp-ms", type=float, default=20.0); ap.add_argument("--gain", type=int, default=100)
ap.add_argument("--diam-arcmin", type=float, default=31.8)
ap.add_argument("--arcsec-per-px", type=float, default=0.288, help="bin-2 scale")
ap.add_argument("--jacobian", default="-23,299.8,-237.5,27.5")
ap.add_argument("--npy", default=None, help="analyse a saved frame instead of taking one")
a = ap.parse_args()

if a.npy:
    img = np.load(a.npy).astype(np.float32)
else:
    p = Pipes(); p.setup("preview", a.exp_ms / 1000.0, a.gain, 2)
    img, w, h, info = p.grab(timeout=40); img = img.astype(np.float32)
    try: p.close()
    except Exception: pass
h, w = img.shape
bias = 3925.0
surf = float(np.percentile(img, 90)) - bias
sky = float(np.percentile(img, 2)) - bias
log("frame %dx%d: p2 %.0f, p90 %.0f over bias" % (w, h, sky, surf))
if surf < 1500:
    sys.exit("no lunar surface in the frame (p90 %.0f over bias)" % surf)
if sky > 0.5 * surf:
    sys.exit("no sky in the frame -- fully on the disc (p2 %.0f, p90 %.0f); the limb is not visible" % (sky, surf))
thr = bias + 0.5 * (sky + surf)
sm = ndimage.gaussian_filter(img, 3)
mask = sm > thr
edge = mask ^ ndimage.binary_erosion(mask, iterations=2)
edge[:3, :] = edge[-3:, :] = False; edge[:, :3] = edge[:, -3:] = False    # frame borders are not limb
ys, xs = np.nonzero(edge)
if xs.size < 200:
    sys.exit("too few limb points (%d)" % xs.size)
sel = np.random.default_rng(0).choice(xs.size, min(xs.size, 4000), replace=False)
xs, ys = xs[sel].astype(float), ys[sel].astype(float)
R = a.diam_arcmin * 60 / a.arcsec_per_px / 2
log("limb points %d, fixed radius %.0f px (%.1f')" % (xs.size, R, a.diam_arcmin / 2))
# RANSAC: two edge points + the known radius give two candidate centres;
# the centre with the most points within 3 px of the circle is the limb --
# crater rims, shadows and the terminator are edges too, but not on that circle.
def resid(c):
    return np.hypot(xs - c[0], ys - c[1]) - R
rng = np.random.default_rng(1)
best_c, best_n = None, 0
P = np.column_stack([xs, ys])
for _ in range(1500):
    i, k = rng.choice(xs.size, 2, replace=False)
    p1, p2 = P[i], P[k]
    d = np.linalg.norm(p2 - p1)
    if d < 40 or d > 2 * R:
        continue
    mid = (p1 + p2) / 2; u = (p2 - p1) / d; nv = np.array([-u[1], u[0]])
    hgt = math.sqrt(max(R * R - (d / 2) ** 2, 0.0))
    for c in (mid + nv * hgt, mid - nv * hgt):
        n = int((np.abs(np.hypot(xs - c[0], ys - c[1]) - R) < 3.0).sum())
        if n > best_n:
            best_n, best_c = n, c
if best_c is None:
    sys.exit("RANSAC found no circle")
log("RANSAC: %d of %d edge points on the best circle" % (best_n, xs.size))
inl = np.abs(resid(best_c)) < 3.0
xs, ys = xs[inl], ys[inl]
c0 = best_c
for it in range(3):
    r = optimize.least_squares(resid, c0); c0 = r.x
    res = np.abs(resid(c0)); keep = res < 3 * max(np.median(res), 0.5)
    xs, ys = xs[keep], ys[keep]
cx, cy = c0
res = resid(c0)
log("disc centre at (%.0f, %.0f) px ; median |residual| %.2f px over %d points" % (cx, cy, float(np.median(np.abs(res))), xs.size))
dx, dy = w / 2 - cx, h / 2 - cy                    # shift needed to bring the centre to the frame middle
dist = math.hypot(cx - w / 2, cy - h / 2) * a.arcsec_per_px / 60
J = np.array([float(v) for v in a.jacobian.split(",")]).reshape(2, 2)
m = np.linalg.solve(J, np.array([dx, dy]))         # arcmin of register RA (on sky), Dec
log("OTA field is %.1f' from the disc centre ; register move to centre the disc: dRA %+.1f' (on sky) dDec %+.1f'  (Jacobian %s)" % (dist, m[0], m[1], a.jacobian))
print("LIMBFIT cx=%.1f cy=%.1f dist_arcmin=%.2f dra_arcmin=%.2f ddec_arcmin=%.2f" % (cx, cy, dist, m[0], m[1]))
if not a.npy:
    fn = save_png(img, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "telemetry", time.strftime("%Y-%m-%d"), "%s_limbfit.png" % time.strftime("%H%M%S")), shrink=2)
    np.save(fn.replace(".png", ".npy"), img.astype(np.uint16)); log("saved %s" % fn)
