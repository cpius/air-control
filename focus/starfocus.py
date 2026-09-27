#!/usr/bin/env python3
"""Focus sweep on ONE star with a drifting, unaligned mount: hot pixels are removed with a 3x3 median
before detection (they fooled planetfocus on 2026-09-16), the star is held near the frame centre with a
measured Jacobian, and the metric is the star's size (equivalent diameter of the >5 sigma region, which
shrinks to a few px at focus). Preview page, bin 2, 16-bit. Parks on the parabola vertex of the V.

    EAF_MIN=15000 EAF_MAX=98000 ASIAIR_HOST=192.168.1.35 python3 -u starfocus.py \
        --lo 57000 --hi 67000 --step 1000 --exp 2 --gain 300 --jacobian "23.0,-299.8,237.5,-27.5"
"""
import argparse, math, os, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daypipes import Pipes, focuser_pos, move_to
from mount import Mount
from session import png

ap = argparse.ArgumentParser()
ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST", "192.168.1.35"))
ap.add_argument("--lo", type=int, required=True); ap.add_argument("--hi", type=int, required=True); ap.add_argument("--step", type=int, required=True)
ap.add_argument("--rounds", type=int, default=1)
ap.add_argument("--exp", type=float, default=2.0); ap.add_argument("--gain", type=int, default=300)
ap.add_argument("--jacobian", required=True, help="px/arcmin bin 2: dx/dRA,dx/dDec,dy/dRA,dy/dDec (measured tonight, same pier side)")
ap.add_argument("--recentre-px", type=float, default=200.0)
ap.add_argument("--min-area", type=int, default=12); ap.add_argument("--nsig", type=float, default=5.0)
ap.add_argument("--park", type=int, default=None, help="park here instead of the vertex")
ap.add_argument("--outdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "telemetry", time.strftime("%Y-%m-%d"), "starfocus"))
a = ap.parse_args(); os.makedirs(a.outdir, exist_ok=True)
J = np.array([float(v) for v in a.jacobian.split(",")]).reshape(2, 2)
def log(s): print(f"{time.strftime('%H:%M:%S')} {s}", flush=True)

def star(img):
    """Largest real blob after a 3x3 median (kills hot pixels). Returns dict or None."""
    f = ndimage.median_filter(img.astype(np.float32), 3)
    bg = float(np.median(f)); sig = max(1.0, 1.4826 * float(np.median(np.abs(f[::4, ::4] - bg))))
    lab, n = ndimage.label(f > bg + a.nsig * sig)
    if not n: return None
    sizes = ndimage.sum(np.ones_like(f), lab, index=range(1, n + 1))
    i = int(np.argmax(sizes)) + 1
    if sizes[i - 1] < a.min_area: return None
    m = lab == i; ys, xs = np.nonzero(m); w = f[m] - bg
    return dict(x=float((xs * w).sum() / w.sum()), y=float((ys * w).sum() / w.sum()), area=int(sizes[i - 1]),
                diam=2.0 * math.sqrt(sizes[i - 1] / math.pi), peak=float(w.max()), flux=float(w.sum()), bg=bg, sig=sig)

p = Pipes(a.host); mt = Mount(a.host)
def mstate():
    global mt
    try:
        return mt.state()
    except Exception:
        mt = Mount(a.host); return mt.state()
def hold(s, w, h):
    global mt
    need = np.array([w / 2 - s["x"], h / 2 - s["y"]])
    if np.hypot(*need) < a.recentre_px: return False
    corr = np.linalg.solve(J, need)
    st = mstate(); dec = st["Dec"]
    ra = st["RA"] + corr[0] / (60.0 * 15.0 * math.cos(math.radians(dec))); de = dec + corr[1] / 60.0
    try:
        mt.goto(ra, de, wait=True, timeout=30)
    except Exception as e:
        log(f"  hold goto raised {e.__class__.__name__}; reconnecting"); mt = Mount(a.host); mt.goto(ra, de, wait=True, timeout=30)
    log(f"  held: star was {np.hypot(*need):.0f} px off -> RA {corr[0]:+.2f}' Dec {corr[1]:+.2f}'")
    return True

positions = list(range(a.lo, a.hi + 1, a.step))
results = {}
try:
    p.setup("preview", a.exp, a.gain, 2)
    for r in range(a.rounds):
        order = positions if r % 2 == 0 else positions[::-1]
        for pos in order:
            move_to(p, pos)
            for attempt in range(3):
                img, w, h, _ = p.grab(timeout=40); img = np.asarray(img).reshape(h, w)
                s = star(img)
                if s is None:
                    log(f"  r{r} EAF {pos}: NO STAR (bg {np.median(img):.0f}) attempt {attempt+1}"); continue
                if hold(s, w, h):
                    continue   # re-grab after a hold
                break
            if s is None:
                continue
            results.setdefault(pos, []).append(s["diam"])
            log(f"r{r} EAF {pos}: star ({s['x']:.0f},{s['y']:.0f}) diam {s['diam']:.1f} px area {s['area']} peak {s['peak']:.0f} flux {s['flux']:.0f}")
            png(img.ravel(), w, h, os.path.join(a.outdir, f"{time.strftime('%H%M%S')}_eaf{pos}.png"), shrink=2)
    if len(results) >= 3:
        xs = np.array(sorted(results)); ys = np.array([np.median(results[x]) for x in xs])
        i = int(np.argmin(ys)); lo, hi = max(0, i - 2), min(len(xs), i + 3)
        best = int(xs[i])
        if hi - lo >= 3:
            c = np.polyfit(xs[lo:hi], ys[lo:hi], 2)
            if c[0] > 0:
                v = -c[1] / (2 * c[0])
                if xs[lo] <= v <= xs[hi - 1]: best = int(round(v))
        log("=== size V: " + "  ".join(f"{int(x)}:{y:.1f}" for x, y in zip(xs, ys)))
        log(f"=== minimum {ys[i]:.1f} px at {int(xs[i])}; parabola vertex {best}")
        if xs[i] in (xs[0], xs[-1]): log("=== minimum is at the EDGE of the range -- extend that way")
        park = a.park or best
        move_to(p, park); log(f"EAF parked at {park}")
finally:
    p.s.close()
