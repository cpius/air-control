#!/usr/bin/env python3
"""Find focus without the target star: step the EAF through --pos, one preview frame each (bin 2),
count compact blobs above the noise and report the sharpest position. Works on any field with a few
stars; at f/25 in the Milky Way a 3 s g300 frame shows stars to ~mag 11 when the focus is close.

    ASIAIR_HOST=192.168.1.35 python3 -u focus/starscan.py --pos 70000,74000,78000,82000,86000,90000,94000,98000 --exp 3 --gain 300
"""
import argparse, os, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from daypipes import Pipes, focuser_pos, move_to
from session import png

ap = argparse.ArgumentParser()
ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST", "192.168.1.35"))
ap.add_argument("--pos", required=True); ap.add_argument("--exp", type=float, default=3.0); ap.add_argument("--gain", type=int, default=300)
ap.add_argument("--nsig", type=float, default=5.0); ap.add_argument("--min-area", type=int, default=12)
ap.add_argument("--outdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "telemetry", time.strftime("%Y-%m-%d"), "starscan"))
a = ap.parse_args(); os.makedirs(a.outdir, exist_ok=True)
def log(s): print(f"{time.strftime('%H:%M:%S')} {s}", flush=True)

def blobs(img):
    f = img.astype(np.float32); sm = ndimage.uniform_filter(f, 3)
    bg = float(np.median(sm)); sig = max(1.0, 1.4826 * float(np.median(np.abs(sm[::3, ::3] - bg))))
    lab, n = ndimage.label(sm > bg + a.nsig * sig)
    out = []
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        m = lab[sl] == i; area = int(m.sum())
        if area < a.min_area: continue
        sub = f[sl]; peak = float(sub[m].max() - bg); h_, w_ = m.shape
        out.append(dict(area=area, peak=peak, w=w_, h=h_, x=sl[1].start + w_ / 2, y=sl[0].start + h_ / 2))
    out.sort(key=lambda b: -b["peak"]); return bg, sig, out

p = Pipes(a.host)
try:
    p.setup("preview", a.exp, a.gain, 2)
    for pos in [int(v) for v in a.pos.split(",")]:
        move_to(p, pos)
        img, w, h, _ = p.grab(timeout=40); img = np.asarray(img).reshape(h, w)
        bg, sig, bl = blobs(img)
        # drop the two fixed hot-pixel clusters
        bl = [b for b in bl if not ((abs(b["x"] - 121) < 15 and abs(b["y"] - 50) < 15) or (abs(b["x"] - 1918) < 15 and abs(b["y"] - 338) < 15))]
        desc = "; ".join(f"({b['x']:.0f},{b['y']:.0f}) peak {b['peak']:.0f} {b['w']}x{b['h']}" for b in bl[:4])
        log(f"EAF {pos}: bg {bg:.0f} sig {sig:.1f}  {len(bl)} blobs  {desc}")
        png(img.ravel(), w, h, os.path.join(a.outdir, f"{time.strftime('%H%M%S')}_eaf{pos}.png"), shrink=2)
finally:
    p.s.close()
