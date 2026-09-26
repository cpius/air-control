#!/usr/bin/env python3
"""Measure the tracking drift on the brightest blob: N frames over --seconds, centroid each,
fit px/min, convert to arcmin/min through --jacobian (px/arcmin, bin 2). Preview page, bin 2.

    ASIAIR_HOST=192.168.1.35 python3 -u blobdrift.py --seconds 60 --exp 0.5 --gain 250 --jacobian "a,b,c,d"
"""
import argparse, os, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daypipes import Pipes

ap = argparse.ArgumentParser()
ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST", "192.168.1.35"))
ap.add_argument("--seconds", type=float, default=60.0); ap.add_argument("--every", type=float, default=10.0)
ap.add_argument("--exp", type=float, default=0.5); ap.add_argument("--gain", type=int, default=250)
ap.add_argument("--jacobian", default=None, help="px/arcmin bin 2: dx/dRA,dx/dDec,dy/dRA,dy/dDec")
a = ap.parse_args()
def log(s): print(f"{time.strftime('%H:%M:%S')} {s}", flush=True)

def centroid(img):
    sm = ndimage.uniform_filter(img.astype(np.float32), 5)
    bg = float(np.median(sm)); sig = max(1.0, 1.4826 * float(np.median(np.abs(sm - bg))))
    m = sm > bg + 8 * sig
    lab, n = ndimage.label(m)
    if not n: return None
    sizes = ndimage.sum(m, lab, range(1, n + 1)); i = int(np.argmax(sizes)) + 1
    ys, xs = np.nonzero(lab == i); w = (sm[lab == i] - bg)
    return float((xs * w).sum() / w.sum()), float((ys * w).sum() / w.sum()), int(sizes[i - 1])

p = Pipes(a.host)
try:
    p.setup("preview", a.exp, a.gain, 2)
    t0 = time.time(); pts = []
    while time.time() - t0 <= a.seconds:
        img, w, h, _ = p.grab(timeout=30); c = centroid(np.asarray(img).reshape(h, w)); t = time.time() - t0
        if c is None: log(f"  {t:5.1f}s no blob"); 
        else: log(f"  {t:5.1f}s blob x={c[0]:.1f} y={c[1]:.1f} area={c[2]} px"); pts.append((t, c[0], c[1]))
        time.sleep(max(0.0, a.every - (time.time() - t0 - t)))
    if len(pts) >= 2:
        T = np.array([q[0] for q in pts]) / 60.0; X = np.array([q[1] for q in pts]); Y = np.array([q[2] for q in pts])
        vx = np.polyfit(T, X, 1)[0]; vy = np.polyfit(T, Y, 1)[0]
        log(f"drift {vx:+.1f} px/min x, {vy:+.1f} px/min y (bin 2) over {pts[-1][0]:.0f}s")
        if a.jacobian:
            J = np.array([float(v) for v in a.jacobian.split(",")]).reshape(2, 2)
            d = np.linalg.solve(J, np.array([vx, vy]))
            log(f"drift in sky terms: RA {d[0]:+.2f}'/min  Dec {d[1]:+.2f}'/min  -> donutfocus --drift-ra {d[0]:.2f} --drift-dec {d[1]:.2f}")
finally:
    p.s.close()
