"""Tip-tilt seeing from satvideo hold CSVs: robust frame-to-frame centroid motion -> r0 -> FWHM.
sigma_axis^2 = 0.17 (lambda/D)^2 (D/r0)^(5/3) (G-tilt); FWHM = 0.98 lambda/r0. lambda 550 nm, D 0.203 m.
Consecutive-frame differences (dt < 1 s) with a MAD estimator, so hold corrections and slow drift drop out.
Mount shake adds to it: an UPPER bound on seeing."""
import csv, glob, os, sys, time, math
import numpy as np
LAM, D = 550e-9, 0.203; R2A = 206265.0
def seeing(path, scale):
    t, x, y = [], [], []
    for r in csv.DictReader(open(path)):
        if r["planet"] == "1" and r["x"]:
            t.append(float(r["t_s"])); x.append(float(r["x"])); y.append(float(r["y"]))
    t, x, y = map(np.array, (t, x, y))
    if len(t) < 30: return None
    dt = np.diff(t); ok = (dt > 0) & (dt < 1.0)
    out = []
    for v in (x, y):
        d = np.diff(v)[ok]; out.append(1.4826 * np.median(np.abs(d - np.median(d))) / math.sqrt(2) * scale)
    sig = math.sqrt((out[0] ** 2 + out[1] ** 2) / 2) / R2A          # rad, per axis
    r0 = D * (0.17 * (LAM / D) ** 2 / sig ** 2) ** (3 / 5)
    return len(t), float(np.median(dt)), out[0], out[1], r0 * 100, 0.98 * LAM / r0 * R2A
rows = []
for p in sorted(glob.glob("*_hold.csv"), key=os.path.getmtime):
    m = time.localtime(os.path.getmtime(p))
    night = time.strftime("%m-%d", time.localtime(os.path.getmtime(p) - 12 * 3600))
    if night not in ("09-26", "09-27"): continue
    scale = 0.1866 if night == "09-27" else 0.0997
    r = seeing(p, scale)
    if r: rows.append((night, time.strftime("%H:%M", m), p) + r)
print("night  end    clip             frames  dt_s  sig_x\"  sig_y\"  r0_cm  seeing_FWHM\"")
for r in rows:
    print("%s  %s  %-16s %5d  %.2f   %.3f   %.3f   %4.1f   %.2f" % r)
