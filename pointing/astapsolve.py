#!/usr/bin/env python3
"""Blind plate solve of a sparse, defocused, drifting frame with ASTAP (D20) via a synthetic star PNG.

Frame .npy -> hot pixels out (isolated outliers + 3x3 median) -> DoG star detection -> the
detections rendered as clean Gaussians into a PNG -> astap_cli. ASTAP's own detector finds
only a handful of stars on these frames and then spirals the whole sky for 15 min; fed our
own detections it solved in 0.2 s (2026-10-12). Never pass -progress on a blind run (363 MB log).

Prints the ICRS (J2000) centre, the measured scale and sky-east on the sensor, and -- given
--target-ra/--target-dec -- the offset of the target from the frame centre.

    python3 -u pointing/astapsolve.py --npy frame.npy --ra 1.187 --dec 35.76 --fov 0.112 --radius 30 \
        --target-ra 1.16219 --target-dec 35.6206
"""
import argparse, math, os, re, subprocess, sys
import numpy as np, cv2
from scipy import ndimage

ASTAP = os.environ.get("ASTAP_DIR", os.path.expanduser("~/ASICAP/astap"))   # the ASTAP install + its D20 database
ap = argparse.ArgumentParser()
ap.add_argument("--npy", required=True)
ap.add_argument("--ra", type=float, required=True, help="search centre RA, hours")
ap.add_argument("--dec", type=float, required=True, help="search centre Dec, deg")
ap.add_argument("--radius", type=float, default=20, help="search radius, deg")
ap.add_argument("--fov", type=float, required=True, help="image HEIGHT in deg (rows x arcsec/px / 3600)")
ap.add_argument("--s1", type=float, default=2); ap.add_argument("--s2", type=float, default=10)
ap.add_argument("--nsig", type=float, default=5); ap.add_argument("--max", type=int, default=40)
ap.add_argument("--edge", type=int, default=20, help="drop detections this close to the frame edge (px)")
ap.add_argument("--target-ra", type=float, default=None, help="ICRS hours: report the target's offset from centre")
ap.add_argument("--target-dec", type=float, default=None)
a = ap.parse_args()

g = np.load(a.npy).astype(float)
med3 = ndimage.median_filter(g, 3)
resid = g - med3; s = 1.4826 * np.median(np.abs(resid - np.median(resid)))
hot = resid > 8 * s                      # isolated single-pixel outliers: hot pixels, cosmics
g[hot] = med3[hot]; g = ndimage.median_filter(g, 3)
log_hot = int(hot.sum())
dog = ndimage.gaussian_filter(g, a.s1) - ndimage.gaussian_filter(g, a.s2)
sig = 1.4826 * np.median(np.abs(dog - np.median(dog)))
lab, n = ndimage.label(dog > a.nsig * sig)
st = []
for i, sl in enumerate(ndimage.find_objects(lab), 1):
    m = lab[sl] == i
    if m.sum() < 6: continue
    w = np.clip(dog[sl], 0, None) * m; f = w.sum(); ys, xs = np.mgrid[sl[0], sl[1]]
    cx, cy = (w * xs).sum() / f, (w * ys).sum() / f
    if min(cx, cy, g.shape[1] - 1 - cx, g.shape[0] - 1 - cy) < a.edge: continue   # stack borders, vignetting edge
    st.append((cx, cy, f))
st.sort(key=lambda s: -s[2]); st = st[:a.max]
print("%d hot pixels replaced; %d stars detected (DoG %.0f-%.0f px, %.0f sigma); brightest: %s"
      % (log_hot, len(st), a.s1, a.s2, a.nsig, " ".join("(%.0f,%.0f)" % (x, y) for x, y, f in st[:8])), flush=True)
if len(st) < 6:
    sys.exit("too few stars to solve (%d) -- longer exposure, or cloud?" % len(st))
H, W = g.shape; syn = np.full((H, W), 1000.0); yy, xx = np.mgrid[0:H, 0:W]
for x, y, f in st:
    # compressed brightness: a linear scale drew the faint stars below ASTAP's own threshold
    # when one bright star dominated (2026-10-14: 18 detections -> "only 2 stars found")
    amp = 4000 + 26000 * (f / st[0][2]) ** 0.25; r = 12
    y0, y1, x0, x1 = max(0, int(y) - r), min(H, int(y) + r + 1), max(0, int(x) - r), min(W, int(x) + r + 1)
    syn[y0:y1, x0:x1] += amp * np.exp(-((xx[y0:y1, x0:x1] - x) ** 2 + (yy[y0:y1, x0:x1] - y) ** 2) / 8.0)
syn += np.random.default_rng(3).normal(0, 20, syn.shape)
base = os.path.splitext(os.path.basename(a.npy))[0] + "_synth"
png = os.path.join(ASTAP, "work", base + ".png")
cv2.imwrite(png, np.clip(syn, 0, 65535).astype(np.uint16))
ini = png[:-4] + ".ini"
if os.path.exists(ini): os.remove(ini)
cmd = [os.path.join(ASTAP, "bin", "astap_cli"), "-f", png, "-fov", "%.4f" % a.fov, "-r", str(a.radius), "-ra", "%.4f" % a.ra,
       "-spd", "%.3f" % (a.dec + 90), "-d", os.path.join(ASTAP, "db"), "-D", "d20", "-z", "1", "-s", "30"]
print("running:", " ".join(cmd), flush=True)
out = subprocess.run(cmd, capture_output=True, text=True, timeout=600).stdout
for line in out.splitlines():
    if re.search(r"stars found|Solution found|Solved in|No solution|quads", line): print("  astap:", line.strip(), flush=True)
if not os.path.exists(ini) or "PLTSOLVD=T" not in open(ini).read():
    sys.exit("NO SOLUTION")
t = open(ini).read(); v = {k: float(x) for k, x in re.findall(r"^(CRVAL[12]|CD[12]_[12]|CRPIX[12])=\s*([-0-9.Ee+]+)", t, re.M)}
CD = np.array([[v["CD1_1"], v["CD1_2"]], [v["CD2_1"], v["CD2_2"]]]); inv = np.linalg.inv(CD)
e = inv @ [1, 0]; e /= np.linalg.norm(e)
scale = 3600 * math.sqrt(abs(np.linalg.det(CD)))
# ASTAP's PNG rows run bottom-up in FITS terms: flip y to get sensor (x right, y down)
print("SOLVED centre ICRS RA %.5f deg (%.5fh) Dec %+.5f ; scale %.4f\"/px ; sky-east on the sensor (%+.3f,%+.3f) (x right, y down)"
      % (v["CRVAL1"], v["CRVAL1"] / 15, v["CRVAL2"], scale, e[0], -e[1]), flush=True)
if a.target_ra is not None:
    dra = ((a.target_ra * 15 - v["CRVAL1"] + 180) % 360 - 180) * math.cos(math.radians(v["CRVAL2"]))
    ddec = a.target_dec - v["CRVAL2"]
    print("TARGET is %+.1f' east %+.1f' north of the frame centre (dRA %+.4fh, dDec %+.4f deg)"
          % (dra * 60, ddec * 60, (a.target_ra * 15 - v["CRVAL1"]) / 15, ddec), flush=True)
