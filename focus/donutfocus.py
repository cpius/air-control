#!/usr/bin/env python3
"""Recover focus when a planet is a defocused disc BIGGER than the frame.

Preview page, bin 2, 16-bit, 0.5 s. Each pass takes a frame and looks for the
disc's edge: threshold halfway between sky and disc, boundary pixels that are
not the frame border, algebraic circle fit. An edge gives the disc's centre
(re-centre it with a goto through the Jacobian) and its radius. No edge means
the whole frame is inside the disc -- then the frame's mean over sky is the
cue: flux is conserved, so the surface brightness RISES as the disc shrinks.

Focus steps of --step are taken in the direction that shrinks the radius (or
raises the brightness); the first step is upward. It stops when the disc is
smaller than --done px across (hand over to planetfocus.py) or the EAF limits
(EAF_MIN/EAF_MAX) are reached.

    EAF_MIN=15000 EAF_MAX=98000 ASIAIR_HOST=... python3 -u focus/donutfocus.py \
        --ra 0.8545 --dec 2.575 --pred-dra -1 --pred-ddec -27 --step 4000
"""
import argparse, math, os, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from daypipes import host, Pipes, log, save_png, move_to, focuser_pos
from mount import Mount

ap = argparse.ArgumentParser()
ap.add_argument("--ra", type=float, required=True); ap.add_argument("--dec", type=float, required=True)
ap.add_argument("--pred-dra", type=float, default=0.0); ap.add_argument("--pred-ddec", type=float, default=0.0)
ap.add_argument("--drift-ra", type=float, default=0.0, help="arcmin/min added to the prediction per minute since --t0 (HH:MM)")
ap.add_argument("--drift-dec", type=float, default=0.0); ap.add_argument("--t0", default=None)
ap.add_argument("--step", type=int, default=4000)
ap.add_argument("--first-dir", type=int, default=1, choices=[1, -1], help="direction of the first EAF step: +1 up (default), -1 down")
ap.add_argument("--max-steps", type=int, default=20)
ap.add_argument("--done", type=float, default=300.0, help="disc diameter (bin-2 px) at which to stop")
ap.add_argument("--exp", type=float, default=0.5); ap.add_argument("--gain", type=int, default=250)
ap.add_argument("--jacobian", default="170.6,39.4,-51.8,180.8", help="px/arcmin bin 2 for the current train and pier side")
ap.add_argument("--sky", type=float, default=None, help="sky+offset level of a planet-free frame (measured if omitted)")
ap.add_argument("--outdir", default="/Users/madsdorup/ASICAP/telemetry/donutfocus")
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)
J = np.array([float(v) for v in a.jacobian.split(",")]).reshape(2, 2)

def analyse(img):
    """Return dict(mean_excess, edge=(cx, cy, R, n, resid) or None, level)."""
    sm = ndimage.gaussian_filter(img.astype(np.float64)[::2, ::2], 3)    # quarter-res for speed
    lo, hi = np.percentile(sm, 2), np.percentile(sm, 99.5)
    out = dict(level=float(np.median(sm)), lo=float(lo), hi=float(hi), edge=None)
    if hi - lo < 60:                       # flat frame: all sky or all disc
        return out
    mask = sm > lo + 0.5 * (hi - lo)
    edge = mask ^ ndimage.binary_erosion(mask)
    edge[:3, :] = edge[-3:, :] = edge[:, :3] = edge[:, -3:] = False
    ys, xs = np.nonzero(edge)
    if len(xs) < 40:
        return out
    A = np.c_[2 * xs, 2 * ys, np.ones_like(xs)]; b = xs ** 2 + ys ** 2
    c, *_ = np.linalg.lstsq(A, b, rcond=None)
    cx, cy = c[0], c[1]; R2 = c[2] + cx ** 2 + cy ** 2
    if R2 <= 0:
        return out
    R = math.sqrt(R2); res = float(np.median(np.abs(np.hypot(xs - cx, ys - cy) - R)))
    if res > 6 or R < 8:
        return out
    out["edge"] = (2 * cx, 2 * cy, 2 * R, len(xs), 2 * res)     # back to bin-2 px
    return out

def target():
    dra, ddec = a.pred_dra, a.pred_ddec
    if a.t0:
        h, mnt = (int(v) for v in a.t0.split(":")); now = time.localtime()
        dt = (now.tm_hour * 60 + now.tm_min + now.tm_sec / 60.0) - (h * 60 + mnt)
        dra += a.drift_ra * dt; ddec += a.drift_dec * dt
        log("drift model: %.1f min since %s -> prediction RA %+.2f' Dec %+.2f'" % (dt, a.t0, dra, ddec))
    return a.ra + dra / (60.0 * 15.0 * math.cos(math.radians(a.dec))), a.dec + ddec / 60.0

m = Mount(host()); p = Pipes()
try:
    st = m.state(); log("start: register RA %.4f Dec %.4f pier %s track %s EAF %s" % (st["RA"], st["Dec"], st.get("pier_side"), st["is_enable_track"], None))
    ra, dec = target(); m.goto(ra, dec, wait=True, timeout=60)
    p.setup("preview", a.exp, a.gain, 2)
    sky = a.sky
    pos = focuser_pos(p); direction = a.first_dir; last_metric = None; n = 0
    while n < a.max_steps:
        img, w, h, info = p.grab(timeout=30)
        r = analyse(img)
        if sky is None:
            sky = r["lo"]                    # first frame: the darkest 2% is the sky if any sky is visible
        excess = r["level"] - sky
        fn = save_png(img, "%s/%s_eaf%d.png" % (a.outdir, time.strftime("%H%M%S"), pos), shrink=2)
        if r["edge"]:
            cx, cy, R, ne, res = r["edge"]
            off = math.hypot(960 - cx, 540 - cy)
            log("EAF %d: DISC edge: centre (%.0f,%.0f) radius %.0f px (diam %.0f px = %.1f') from %d pts (resid %.1f) ; %.0f px off centre ; level %.0f excess %.0f ; %s" % (
                pos, cx, cy, R, 2 * R, 2 * R * 0.294 / 60, ne, res, off, r["level"], excess, fn))
            metric = -R                        # bigger is better = smaller radius
            if off > 120:
                need = np.array([960 - cx, 540 - cy]); corr = np.linalg.solve(J, need)
                corr = np.clip(corr, -8, 8)
                st = m.state()
                m.goto(st["RA"] + corr[0] / (60.0 * 15.0 * math.cos(math.radians(st["Dec"]))), st["Dec"] + corr[1] / 60.0, wait=True, timeout=30)
                log("   re-centred the disc: RA %+.2f' Dec %+.2f'" % (corr[0], corr[1]))
            if 2 * R < a.done:
                log("disc is %.0f px across -- small enough for planetfocus.py; EAF at %d" % (2 * R, pos)); break
        else:
            log("EAF %d: no edge (level %.0f, lo %.0f, hi %.0f, excess over sky %.0f) -> %s ; %s" % (
                pos, r["level"], r["lo"], r["hi"], excess, "inside the disc" if excess > 30 else "empty sky?!", fn))
            metric = excess if excess > 30 else None
        if last_metric is not None and metric is not None and metric < last_metric - 1e-9:
            direction = -direction
            log("   metric fell (%.1f -> %.1f): reversing to %+d" % (last_metric, metric, direction))
        if metric is not None:
            last_metric = metric
        nxt = pos + direction * a.step
        try:
            pos = move_to(p, nxt)
        except ValueError as e:
            log("EAF limit: %s" % e); break
        p.grab(timeout=30)                    # discard the frame that may predate the move
        n += 1
finally:
    p.close(); m.close(); log("closed at EAF %s" % pos)
