#!/usr/bin/env python3
"""Find the Moon's disc when the field shows only glare: a square spiral of
register GOTOs around the current pointing, one preview frame per point, stop
at the first frame that shows lunar surface.

    ASIAIR_HOST=192.168.1.36 python3 -u moon/moonspiral.py --spacing 20 --rings 3 --exp-ms 50 --gain 100

Why gotos and not joystick nudges (moonclimb.py): the first nudge after a
direction change under-delivers (0.3' asked 8', 2026-09-27), so a probe that
"did not brighten" may simply not have moved. A goto lands to ~0.1' of the
register target whatever the register's offset from the sky.

With --spacing 20 every point of the plane is within 14' of a grid point, so a
disc of radius 16' inside the searched square is always hit.
"""
import argparse, json, math, os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from daypipes import host, Pipes, log, save_png
from mount import Mount

ap = argparse.ArgumentParser()
ap.add_argument("--exp-ms", type=float, default=50.0); ap.add_argument("--gain", type=int, default=100)
ap.add_argument("--spacing", type=float, default=20.0, help="grid spacing, arcmin on sky")
ap.add_argument("--rings", type=int, default=3, help="square rings around the start (3 -> 7x7 = 49 points)")
ap.add_argument("--disc", type=float, default=4000.0, help="clipped-mean level (ADU over bias) meaning surface fills the frame")
ap.add_argument("--min-alt", type=float, default=8.0)
ap.add_argument("--outdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "telemetry", time.strftime("%Y-%m-%d"), "moonspiral"))
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)

def cmean(img):
    lo, hi = np.percentile(img, [0.5, 99.5]); return float(img[(img >= lo) & (img <= hi)].mean())

def spiral(rings):
    pts = [(0, 0)]
    for r in range(1, rings + 1):
        x, y = r, -r + 1                         # walk the ring: up the east side, west along the top, down, east along the bottom
        ring = [(r, k) for k in range(-r + 1, r + 1)] + [(k, r) for k in range(r - 1, -r - 1, -1)] + \
               [(-r, k) for k in range(r - 1, -r - 1, -1)] + [(k, -r) for k in range(-r + 1, r + 1)]
        pts += ring
    return pts

m = Mount(host()); p = Pipes()
t0 = time.time()
rec = []
try:
    s = m.state()
    ra0, dec0 = s["RA"], s["Dec"]
    log("start register RA %.4f h Dec %+.4f  Alt %.2f Az %.2f pier %s track %s" % (ra0, dec0, s["Alt"], s["Az"], s["pier_side"], s["is_enable_track"]))
    p.setup("preview", 0.001, a.gain, 2)
    img, *_ = p.grab(timeout=40); BIAS = cmean(img); log("bias %.0f" % BIAS)
    p.setup("preview", a.exp_ms / 1000.0, a.gain, 2)
    pts = spiral(a.rings)
    log("%d points, spacing %.0f', square +-%.0f'" % (len(pts), a.spacing, a.rings * a.spacing))
    found = None
    for i, (ix, iy) in enumerate(pts):
        dx, dy = ix * a.spacing, iy * a.spacing          # arcmin east / north on sky
        ra = ra0 + dx / 60.0 / 15.0 / math.cos(math.radians(dec0)); dec = dec0 + dy / 60.0
        if i:
            m.goto(ra, dec, timeout=60)
            time.sleep(0.5)
        s = m.state()
        if s["Alt"] < a.min_alt:
            log("  (%+4.0f,%+4.0f)' alt %.1f below --min-alt, skipped" % (dx, dy, s["Alt"])); continue
        if not s["is_enable_track"]:
            log("  tracking was OFF -- re-enabling"); m.set_tracking(True)
        img, w, h, info = p.grab(timeout=40)
        img = img.astype(np.float32)
        lvl = cmean(img) - BIAS
        frac = float((img - BIAS > a.disc).mean())
        gx = float(img[:, -w // 3:].mean() - img[:, :w // 3].mean()); gy = float(img[-h // 3:, :].mean() - img[:h // 3, :].mean())
        fn = save_png(img, os.path.join(a.outdir, "%s_%+04.0f_%+04.0f.png" % (time.strftime("%H%M%S"), dx, dy)), shrink=2)
        rec.append(dict(t=time.time() - t0, dx=dx, dy=dy, ra=s["RA"], dec=s["Dec"], alt=s["Alt"], az=s["Az"], level=lvl, frac=frac, gx=gx, gy=gy, png=os.path.basename(fn)))
        log("  [%2d/%d %5.0fs] E%+4.0f' N%+4.0f'  level %7.1f  bright-frac %.3f  grad x %+7.1f y %+7.1f  max %5.0f" %
            (i + 1, len(pts), time.time() - t0, dx, dy, lvl, frac, gx, gy, img.max()))
        if lvl > a.disc or 0.05 < frac:
            found = rec[-1]; log("SURFACE at E%+.0f' N%+.0f' (register RA %.4f Dec %+.4f): level %.0f, %.0f%% bright" % (dx, dy, s["RA"], s["Dec"], lvl, 100 * frac))
            break
    if not found:
        best = max(rec, key=lambda r: r["level"])
        log("no surface in %d points; brightest E%+.0f' N%+.0f' level %.0f -- going there" % (len(rec), best["dx"], best["dy"], best["level"]))
        m.goto(ra0 + best["dx"] / 60.0 / 15.0 / math.cos(math.radians(dec0)), dec0 + best["dy"] / 60.0, timeout=60)
finally:
    with open(os.path.join(a.outdir, time.strftime("%H%M%S") + "_spiral.json"), "w") as f:
        json.dump(rec, f, indent=1)
    try: p.close()
    except Exception: pass
    m.close()
