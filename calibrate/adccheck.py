#!/usr/bin/env python3
"""ADC tuning by numbers: red-minus-blue centroid offset of the brightest star on a 16-bit RGGB frame.

Takes N frames on the given page (focus page bin 1 = 1:1 centre crop, best), splits the Bayer planes,
centroids the star in R and B inside a box around its peak, and prints the R-B offset in bin-1 px and
arcsec (--arcsec-per-px). Zero means the ADC is set. The offset's direction tells which control to move:
the component along the vertical (zenith) axis is the lever opening, the perpendicular one is the roll.

    ASIAIR_HOST=192.168.1.35 python3 -u adccheck.py --page focus --exp 0.05 --gain 100 --frames 5 --arcsec-per-px 0.107
"""
import argparse, os, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daypipes import Pipes

ap = argparse.ArgumentParser()
ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST", "192.168.1.35"))
ap.add_argument("--page", default="focus", choices=["focus", "preview"])
ap.add_argument("--exp", type=float, default=0.05); ap.add_argument("--gain", type=int, default=100)
ap.add_argument("--frames", type=int, default=5); ap.add_argument("--box", type=int, default=40, help="half-size of the centroid box, plane px")
ap.add_argument("--arcsec-per-px", type=float, default=0.107, help="bin-1 scale (barlow+ADC estimate 0.107)")
ap.add_argument("--pattern", default="RGGB")
a = ap.parse_args()
def log(s): print(f"{time.strftime('%H:%M:%S')} {s}", flush=True)

def planes(img):
    pat = a.pattern.upper(); out = {}
    for k, (dy, dx) in zip(pat, [(0, 0), (0, 1), (1, 0), (1, 1)]):
        out.setdefault(k, []).append(img[dy::2, dx::2].astype(np.float32))
    return {k: (v[0] if len(v) == 1 else (v[0] + v[1]) / 2) for k, v in out.items()}

def cen(pl, cx, cy):
    y0, y1 = max(0, cy - a.box), min(pl.shape[0], cy + a.box + 1); x0, x1 = max(0, cx - a.box), min(pl.shape[1], cx + a.box + 1)
    sub = pl[y0:y1, x0:x1]; bg = float(np.median(np.concatenate([sub[0], sub[-1], sub[:, 0], sub[:, -1]])))
    w = np.clip(sub - bg, 0, None); w[w < 0.05 * w.max()] = 0
    ys, xs = np.mgrid[y0:y1, x0:x1]
    return float((xs * w).sum() / w.sum()), float((ys * w).sum() / w.sum()), float(sub.max() - bg)

p = Pipes(a.host); res = []
try:
    p.setup(a.page, a.exp, a.gain, 1)
    for i in range(a.frames):
        img, w, h, _ = p.grab(timeout=30); img = np.asarray(img).reshape(h, w)
        pl = planes(img); g = ndimage.uniform_filter(pl["G"], 3)
        cy, cx = np.unravel_index(int(np.argmax(g)), g.shape)
        r = cen(pl["R"], cx, cy); b = cen(pl["B"], cx, cy); gg = cen(pl["G"], cx, cy)
        dx, dy = (r[0] - b[0]) * 2, (r[1] - b[1]) * 2     # plane px -> sensor px
        res.append((dx, dy))
        log(f"  frame {i+1}: star at G ({cx*2},{cy*2}) peaks R {r[2]:.0f} G {gg[2]:.0f} B {b[2]:.0f}  R-B offset dx {dx:+.2f} dy {dy:+.2f} px")
    d = np.median(np.array(res), axis=0); s = a.arcsec_per_px
    log(f"R-B offset (median of {len(res)}): dx {d[0]:+.2f} dy {d[1]:+.2f} px = {d[0]*s:+.2f}\" {d[1]*s:+.2f}\"  |{np.hypot(*d)*s:.2f}\"|  (+x right, +y down on the sensor)")
finally:
    p.s.close()
