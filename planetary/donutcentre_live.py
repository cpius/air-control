#!/usr/bin/env python3
"""Centre a planet from its DEFOCUSED donut, when the in-focus disc may be outside the field.

Defocus to --eaf (disc a few arcmin, may hang off the frame), fit a circle to the donut's OUTER edge
(robust to a partial disc and to the secondary's shadow), optionally measure sky-east on the sensor with a
tracking pause, then nudge (slowpulse.Nudger) until the donut centre sits on the frame centre. Ends at
--eaf-focus and grabs one short frame to confirm the planet is in the field.

    ASIAIR_HOST=192.168.1.39 EAF_MIN=20000 EAF_MAX=70000 python3 -u planetary/donutcentre_live.py --eaf 47000 --eaf-focus 55523
"""
import argparse, math, os, sys, time
import numpy as np
from scipy import ndimage
AC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # the repo root
sys.path.insert(0, os.path.join(AC, "lib"))
from daypipes import Pipes, log, save_png, move_to, focuser_pos
from slowpulse import Nudger
from air_rpc import Air
try:
    from recorder import rec
except Exception:
    rec = None
ap = argparse.ArgumentParser()
ap.add_argument("--eaf", type=int, required=True, help="defocused EAF position for centring")
ap.add_argument("--eaf-focus", type=int, required=True, help="EAF position to finish at")
ap.add_argument("--exp", type=float, default=1.0); ap.add_argument("--gain", type=int, default=300)
ap.add_argument("--scale", type=float, default=0.162, help="bin-1 arcsec/px (replaced by the pause measurement)")
ap.add_argument("--east", default=None, help="sky east on the sensor x,y; omit to measure with a tracking pause")
ap.add_argument("--pause", type=float, default=8.0, help="tracking pause for the angle measurement, s")
ap.add_argument("--dec", type=float, default=1.15, help="target Dec, deg (pause drift = 15.04 cos Dec arcsec/s)")
ap.add_argument("--passes", type=int, default=4); ap.add_argument("--tol", type=float, default=15.0, help="arcsec")
ap.add_argument("--final-exp", type=float, default=0.05)
ap.add_argument("--tag", default="dc")
a = ap.parse_args()
HOST = os.environ["ASIAIR_HOST"]
OUT = os.path.expanduser("~/ASICAP/telemetry/%s" % time.strftime("%Y-%m-%d")); os.makedirs(OUT, exist_ok=True)
CX, CY = 1920.0, 1080.0                     # bin-1 frame centre

def track(on):
    m = Air(HOST, 4400, timeout=15)
    try:
        m.call("scope_set_track_state", [bool(on)])
        return m.call("scope_get_info", [])["result"].get("is_enable_track")
    finally:
        m.close()

def donut(img, tag):
    """outer-edge circle fit on a bin-2 frame -> (cx, cy, R) in bin-1 px, plus coverage."""
    sm = ndimage.gaussian_filter(img.astype(np.float64), 3)
    sky = float(np.percentile(sm, 5)); top = float(np.percentile(sm, 99))
    if top - sky < 300:                         # 2026-11-04: a 40-ADU test fitted a "donut" to a 70-ADU sky gradient
        log("  %s: no donut (range %.0f ADU)" % (tag, top - sky)); return None
    mask = sm > sky + 0.35 * (top - sky)
    lab, n = ndimage.label(mask)
    if not n:
        return None
    big = int(np.argmax(ndimage.sum(mask, lab, range(1, n + 1)))) + 1
    mask = ndimage.binary_fill_holes(lab == big)
    edge = mask & ~ndimage.binary_erosion(mask, iterations=2)
    h, w = mask.shape
    ys, xs = np.nonzero(edge)
    keep = (xs > 4) & (xs < w - 5) & (ys > 4) & (ys < h - 5)
    xs, ys = xs[keep].astype(float), ys[keep].astype(float)
    if len(xs) < 50:
        log("  %s: edge too short (%d px)" % (tag, len(xs))); return None
    sel = np.ones(len(xs), bool)
    for _ in range(4):                       # Kasa fit, then drop the secondary-shadow (inner) edge points
        A = np.c_[2 * xs[sel], 2 * ys[sel], np.ones(sel.sum())]
        bb = xs[sel] ** 2 + ys[sel] ** 2
        (cx, cy, c0), *_ = np.linalg.lstsq(A, bb, rcond=None)
        R = math.sqrt(max(c0 + cx * cx + cy * cy, 1.0))
        r = np.hypot(xs - cx, ys - cy)
        sel = np.abs(r - R) < 0.08 * R
        if sel.sum() < 30:
            break
    resid = float(np.median(np.abs(np.hypot(xs[sel] - cx, ys[sel] - cy) - R))) if sel.sum() else 99.0
    v = np.clip((sm - sky) / max(top - sky, 1), 0, 1)
    png = os.path.join(OUT, "%s_%s.png" % (a.tag, tag)); save_png((v * 65535).astype(np.float32), png, 1)
    if rec:
        try: rec.artifact("preview", png)
        except Exception: pass
    log("  %s: donut centre bin1 (%.0f, %.0f) R %.0f px, %d edge px, fit resid %.1f px, disc covers %.0f%% of frame -> %s" % (
        tag, cx * 2, cy * 2, R * 2, int(sel.sum()), resid, 100 * mask.mean(), png))
    return cx * 2, cy * 2, R * 2

p = Pipes()
try:
    log("tracking %s" % track(True))
    p.setup("preview", a.exp, a.gain, 2)
    log("EAF %d -> %d (defocus for centring)" % (focuser_pos(p), a.eaf)); move_to(p, a.eaf)
    p.grab(timeout=40)                                              # first frame after a move is suspect
    img, w, h, info = p.grab(timeout=40); c = donut(img, "0")
    if c is None:
        sys.exit("no donut in the first frame")
    scale = a.scale
    if a.east:
        E = np.array([float(v) for v in a.east.split(",")]); E /= np.linalg.norm(E)
    else:
        log("tracking pause %.1f s to measure sky-east on the sensor" % a.pause)
        track(False); t = time.time(); time.sleep(a.pause); track(True); dt = time.time() - t
        time.sleep(1.0); p.grab(timeout=40)
        img, *_ = p.grab(timeout=40); c2 = donut(img, "pause")
        if c2 is None:
            sys.exit("donut lost after the tracking pause")
        d = np.array([c2[0] - c[0], c2[1] - c[1]]); moved = 15.041 * dt * math.cos(math.radians(a.dec))
        E = -d / np.linalg.norm(d); scale = moved / np.linalg.norm(d)
        log("pause %.1f s -> pointing %.0f\" east; image moved (%.0f, %.0f) px -> east on sensor (%.3f, %.3f), scale %.3f\"/px (assumed %.3f)" % (
            dt, moved, d[0], d[1], E[0], E[1], scale, a.scale))
        if not 0.6 * a.scale < scale < 1.6 * a.scale:
            log("  scale implausible -- keeping %.3f" % a.scale); scale = a.scale
        c = c2
    N = np.array([-E[1], E[0]])
    nud = Nudger(HOST, log=log)
    for k in range(a.passes):
        d = np.array([c[0] - CX, c[1] - CY]); e_as, n_as = float(d @ E) * scale, float(d @ N) * scale
        log("pass %d: donut %.0f\" east %+.0f\" north of centre" % (k, e_as, n_as))
        if abs(e_as) < a.tol and abs(n_as) < a.tol:
            log("centred"); break
        done = nud.nudge(e_as, n_as); log("  nudged %s" % ", ".join("%s %.0f\" (rate %d, %.1fs)" % q for q in done))
        log("  tracking %s" % track(True))
        time.sleep(1.0); p.grab(timeout=40)
        img, *_ = p.grab(timeout=40); cn = donut(img, "p%d" % (k + 1))
        if cn is None:
            sys.exit("donut lost after the nudge")
        dn = np.array([cn[0] - CX, cn[1] - CY]); e2, n2 = float(dn @ E) * scale, float(dn @ N) * scale
        if k == 0 and abs(n_as) > 60 and abs(n2) > abs(n_as) * 1.3:   # north/south went the wrong way: mirrored sensor
            N = -N; log("  Dec moved the wrong way -> flipping north on the sensor"); e2, n2 = float(dn @ E) * scale, float(dn @ N) * scale
        c = cn
    log("EAF %d -> %d (focus)" % (focuser_pos(p), a.eaf_focus)); move_to(p, a.eaf_focus)
    p.close(); p = Pipes(); p.setup("preview", a.final_exp, a.gain, 2); p.grab(timeout=40)
    img, *_ = p.grab(timeout=40); g = img.astype(np.float64)
    sm = ndimage.gaussian_filter(g, 2); sky = float(np.median(sm)); pk = float(sm.max())
    yy, xx = np.unravel_index(int(np.argmax(sm)), sm.shape)
    m = sm > sky + 0.3 * (pk - sky); cyx = ndimage.center_of_mass(m)
    np.save(os.path.join(OUT, "%s_focus.npy" % a.tag), img.astype(np.uint16))
    v = np.clip((sm - sky) / max(pk - sky, 1), 0, 1) ** 0.5
    png = os.path.join(OUT, "%s_focus.png" % a.tag); save_png((v * 65535).astype(np.float32), png, 1)
    if rec:
        try: rec.artifact("preview", png)
        except Exception: pass
    log("FOCUS FRAME %.3fs: sky %.0f peak %.0f (+%.0f) at bin2 (%d, %d); blob %d px centred bin1 (%.0f, %.0f) -> %s" % (
        a.final_exp, sky, pk, pk - sky, xx, yy, int(m.sum()), cyx[1] * 2, cyx[0] * 2, png))
    log("RESULT east=%.3f,%.3f scale=%.4f" % (E[0], E[1], scale))
finally:
    try: p.close()
    except Exception: pass
