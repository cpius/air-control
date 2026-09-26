#!/usr/bin/env python3
"""Grid-search for a bright, grossly defocused star (or planet) with a tiny field: goto a grid of
register offsets around the target, one preview frame each, stop at the first BIG blob or a frame
whose median jumps (the donut covers the whole frame). No centring, no sync -- it reports the offset
where the blob was seen so donutfocus.py can take over with its circle fit.

    ASIAIR_HOST=192.168.1.35 python3 -u search.py --ra 0.1628 --dec 29.239 --name Alpheratz \
        --search-ra 48 --search-dec 36 --step-ra 12 --step-dec 9 --exp 2 --gain 250
"""
import argparse, math, os, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session import png
from daypipes import Pipes
from mount import Mount

ap = argparse.ArgumentParser()
ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST", "192.168.1.35"))
ap.add_argument("--ra", type=float, required=True, help="JNow hours"); ap.add_argument("--dec", type=float, required=True, help="JNow deg")
ap.add_argument("--name", default="target")
ap.add_argument("--search-ra", type=float, default=48.0); ap.add_argument("--search-dec", type=float, default=36.0)
ap.add_argument("--step-ra", type=float, default=12.0); ap.add_argument("--step-dec", type=float, default=9.0)
ap.add_argument("--pred-dra", type=float, default=0.0); ap.add_argument("--pred-ddec", type=float, default=0.0)
ap.add_argument("--exp", type=float, default=2.0); ap.add_argument("--gain", type=int, default=250); ap.add_argument("--bin", type=int, default=2)
ap.add_argument("--min-area", type=int, default=15000, help="blob area in bin px that counts as the donut")
ap.add_argument("--nsig", type=float, default=6.0)
ap.add_argument("--dec-rows", default=None, help="explicit comma list of Dec offsets (arcmin) instead of the +/-search-dec grid")
ap.add_argument("--min-blobs", type=int, default=999, help="also count as found when this many blobs (>=8 px) exceed nsig: a faint ring fragments into many")
ap.add_argument("--offsets", default=None, help="explicit list of RA,Dec offsets in arcmin, e.g. '0,-250 -12,-240 ...' (overrides the grid, searched in the given order)")
ap.add_argument("--no-jump", action="store_true", help="ignore frame-median jumps (passing cloud brightens the whole frame)")
ap.add_argument("--outdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "telemetry", time.strftime("%Y-%m-%d"), "search"))
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)
def log(s): print(f"{time.strftime('%H:%M:%S')} {s}", flush=True)

def cmd(dra_m, ddec_m):
    return (a.ra + (a.pred_dra + dra_m) / (60.0 * 15.0 * math.cos(math.radians(a.dec))), a.dec + (a.pred_ddec + ddec_m) / 60.0)

def analyse(img):
    sm = img.astype(np.float32).reshape(img.shape[0] // 4, 4, img.shape[1] // 4, 4).mean(axis=(1, 3))
    bg = float(np.median(sm)); sig = max(0.5, 1.4826 * float(np.median(np.abs(sm - bg))))
    lab, n = ndimage.label(ndimage.uniform_filter(sm, 3) > bg + a.nsig * sig)
    best = None
    if n:
        sizes = ndimage.sum(np.ones_like(sm), lab, index=range(1, n + 1))
        nb = int((np.asarray(sizes) * 16 >= 8).sum())
        i = int(np.argmax(sizes)) + 1
        m = lab == i; ys, xs = np.nonzero(m)
        best = dict(area=int(sizes[i - 1]) * 16, x=float(xs.mean()) * 4 + 2, y=float(ys.mean()) * 4 + 2,
                    excess=float(sm[m].mean() - bg), peak=float(sm[m].max() - bg), nblobs=nb)
    return bg, sig, best

pts = []
nra, ndec = int(a.search_ra // a.step_ra), int(a.search_dec // a.step_dec)
rows = [float(v) for v in a.dec_rows.split(",")] if a.dec_rows else [j * a.step_dec for j in range(-ndec, ndec + 1)]
for i in range(-nra, nra + 1):
    for ddec in rows:
        pts.append((i * a.step_ra, ddec))
pts.sort(key=lambda q: math.hypot(q[0], q[1] * 1.3))
if a.offsets:
    pts = [tuple(float(v) for v in tok.split(",")) for tok in a.offsets.split()]
log(f"{a.name}: {len(pts)} grid points, +/-{a.search_ra:.0f}' RA x +/-{a.search_dec:.0f}' Dec, steps {a.step_ra:.0f}'x{a.step_dec:.0f}', {a.exp}s g{a.gain} bin{a.bin}")

p = Pipes(a.host)
mt = Mount(a.host)
def goto(ra, dec):
    global mt
    try:
        mt.goto(ra, dec, wait=True, timeout=60)
    except Exception as e:
        log(f"  goto raised {e.__class__.__name__}: reconnecting the mount"); mt = Mount(a.host); mt.goto(ra, dec, wait=True, timeout=60)
base_med = None
try:
    p.setup("preview", a.exp, a.gain, a.bin)
    t0 = time.time()
    for k, (dra_m, ddec_m) in enumerate(pts, 1):
        ra_c, dec_c = cmd(dra_m, ddec_m)
        tg = time.time(); goto(ra_c, dec_c); tg = time.time() - tg
        img, w, h, _ = p.grab(timeout=30); img = np.asarray(img).reshape(h, w); v = img.ravel()
        bg, sig, b = analyse(img)
        if base_med is None: base_med = bg
        jump = bg - base_med
        desc = f"{b['nblobs']} blobs, biggest {b['area']} px at x={b['x']:.0f} y={b['y']:.0f} excess {b['excess']:.0f}" if b else "no blob"
        log(f"  {k:3d}/{len(pts)} RA {dra_m:+4.0f}' Dec {ddec_m:+4.0f}': goto {tg:.1f}s, median {bg:.0f} ({jump:+.0f} vs first, sig {sig:.0f}), {desc}   [{time.time()-t0:.0f}s]")
        found = (b is not None and (b["area"] >= a.min_area or b["nblobs"] >= a.min_blobs)) or (not a.no_jump and jump > 8 * sig and jump > 40)
        if found:
            st = mt.state()
            fn = os.path.join(a.outdir, f"{time.strftime('%H%M%S')}_{a.name}_found.png"); png(v, w, h, fn, shrink=2)
            log(f"FOUND {a.name} at offset RA {a.pred_dra + dra_m:+.0f}' Dec {a.pred_ddec + ddec_m:+.0f}' (register RA {st['RA']:.4f} Dec {st['Dec']:.4f}, pier {st.get('pier_side')}); {desc}; frame median jump {jump:+.0f}; png {os.path.relpath(fn)}")
            print(f"RESULT pred_dra={a.pred_dra + dra_m:.1f} pred_ddec={a.pred_ddec + ddec_m:.1f} x={b['x'] if b else -1:.0f} y={b['y'] if b else -1:.0f}")
            sys.exit(0)
    log("nothing found on the grid"); sys.exit(1)
finally:
    p.s.close()
