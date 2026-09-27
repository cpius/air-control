#!/usr/bin/env python3
"""One preview frame (16-bit, bin 2 by default): stats, the biggest blobs, a PNG. stop_exposure first.

    ASIAIR_HOST=192.168.1.35 python3 -u blobgrab.py --exp 1 --gain 150 --bin 2 --tag alpheratz
"""
import argparse, os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session import Session, png
from findstar import blobs

ap = argparse.ArgumentParser()
ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST", "192.168.1.35"))
ap.add_argument("--exp", type=float, default=1.0); ap.add_argument("--gain", type=int, default=150)
ap.add_argument("--bin", type=int, default=2); ap.add_argument("--nsig", type=float, default=8.0)
ap.add_argument("--tag", default="grab"); ap.add_argument("--outdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "telemetry", time.strftime("%Y-%m-%d")))
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)
s = Session(a.host, os.path.join(os.path.dirname(os.path.abspath(__file__)), "embedded_key.pem"), with_mount=False)
try:
    t0 = time.time()
    s.page("preview", a.exp, a.gain, binning=a.bin)
    v, w, h = s.fresh(2)
    img = np.asarray(v).reshape(h, w)
    med = float(np.median(img)); p999 = float(np.percentile(img, 99.9))
    print(f"{time.strftime('%H:%M:%S')} frame {w}x{h} bin{a.bin} {a.exp}s g{a.gain} in {time.time()-t0:.1f}s: median {med:.0f} p99 {np.percentile(img,99):.0f} p99.9 {p999:.0f} max {int(img.max())}  mean-median {float(img.mean())-med:+.1f}")
    bg, sig, bl = blobs(img, nsig=a.nsig, top=5)
    if bl:
        for b in bl:
            print(f"  blob x={b['x']:.0f} y={b['y']:.0f} (bin{a.bin} px, centre {w/2:.0f},{h/2:.0f})  flux={b['flux']:.0f} peak={b['peak']:.0f} area={b['area_spx']} spx")
    else:
        print(f"  no blobs above {a.nsig} sigma (bg {bg:.0f}, sig {sig:.1f})")
    p = os.path.join(a.outdir, f"{time.strftime('%H%M%S')}_{a.tag}.png")
    png(v, w, h, p, shrink=2); print("  png", os.path.relpath(p))
finally:
    s.close()
