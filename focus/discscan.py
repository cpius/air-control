#!/usr/bin/env python3
"""Gross-defocus star disc: goto offsets x EAF positions, fit the disc edge in each preview frame.

A star far out of focus is a faint uniform disc hundreds of px across, a few tens of ADU over sky.
Block-average 8x8, smooth, and measure the donut two ways: a circle fit to the half-level edge
(confused by the faint halo around the ring and by neighbouring donuts -- residuals of 30-80 px),
and the radial profile around the smoothed peak (R_out at half the ring level; the steadier one).
Diameter vs EAF is linear on each side of focus, so three positions give the slope and an
extrapolated focus. Each frame also gets a stretched *_glow.png -- look at it; one glance beats
either number.

2026-10-19, reducer back in, preview bin 2 5 s g200: ring diameters 561/744/900 px at
39270/44270/49270 = 0.034 bin-2 px/step shrinking DOWN; extrapolated zero ~22800, which the
multi-star sweep (focus/fieldsweep.py) then confirmed exactly. --offsets finds the donut when the
register is off: a ring at the frame edge moves the predicted way under a 12' goto. The donut you
centre is not necessarily your target star -- plate solve once in focus.

    ASIAIR_HOST=... EAF_MIN=0 EAF_MAX=100000 python3 -u focus/discscan.py --ra 5.311 --dec 46.025 \
        --offsets "0,0;0,12;12,0" --exp 5 --gain 200            # where is the donut?
    ASIAIR_HOST=... EAF_MIN=0 EAF_MAX=100000 python3 -u focus/discscan.py --ra 5.311 --dec 46.025 \
        --offsets 12,0 --eafs 44270,39270,49270 --exp 5 --gain 200   # which way, how far?
"""
import argparse, math, os, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from daypipes import Pipes, log, move_to, focuser_pos, host
from mount import Mount

ap = argparse.ArgumentParser()
ap.add_argument("--ra", type=float, required=True, help="target JNow hours")
ap.add_argument("--dec", type=float, required=True, help="target JNow deg")
ap.add_argument("--offsets", default="0,0", help="'east,north;...' arcmin on the sky; goto only when it changes")
ap.add_argument("--no-goto", action="store_true", help="never goto (use wherever the mount is)")
ap.add_argument("--eafs", default="", help="comma list of EAF positions (empty = stay)")
ap.add_argument("--exp", type=float, default=5.0); ap.add_argument("--gain", type=int, default=200)
ap.add_argument("--frames", type=int, default=1)
ap.add_argument("--tag", default="disc")
ap.add_argument("--outdir", default=os.path.expanduser("~/ASICAP/telemetry/%s/frames" % time.strftime("%Y-%m-%d")))
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)
B = 8


def analyse(img):
    h, w = img.shape
    bl = img[:h // B * B, :w // B * B].reshape(h // B, B, w // B, B).mean(axis=(1, 3))
    sm = ndimage.gaussian_filter(bl, 1.5)
    sky, top = np.percentile(sm, 5), np.percentile(sm, 99)
    noise = 1.4826 * np.median(np.abs(np.diff(bl, axis=1))) / math.sqrt(2) / 1.5   # rough, after smoothing
    out = dict(sky=float(sky), top=float(top), contrast=float(top - sky), noise=float(noise), edge=None, frac=0.0)
    if top - sky < 6 * noise:
        return out, sm
    mask = sm > sky + 0.5 * (top - sky)
    lab, n = ndimage.label(mask)
    if n == 0:
        return out, sm
    sizes = ndimage.sum(mask, lab, range(1, n + 1))
    mask = lab == (1 + int(np.argmax(sizes)))
    out["frac"] = float(mask.mean())
    out["excess"] = float(sm[mask].mean() - np.median(sm[~mask])) if (~mask).any() else float("nan")
    edge = mask ^ ndimage.binary_erosion(mask)
    edge[:2, :] = edge[-2:, :] = edge[:, :2] = edge[:, -2:] = False
    ys, xs = np.nonzero(edge)
    if len(xs) < 12:
        return out, sm
    A = np.c_[2 * xs, 2 * ys, np.ones_like(xs)]; b = xs ** 2 + ys ** 2
    c, *_ = np.linalg.lstsq(A, b.astype(float), rcond=None)
    cx, cy = c[0], c[1]; R2 = c[2] + cx ** 2 + cy ** 2
    if R2 <= 0:
        return out, sm
    R = math.sqrt(R2)
    resid = float(np.std(np.hypot(xs - cx, ys - cy) - R))
    out["edge"] = (cx * B, cy * B, R * B, len(xs), resid * B)     # bin-2 px
    return out, sm


def radial(img):
    """Donut centre = peak of a heavily smoothed block image; outer/inner radius from the radial profile."""
    h, w = img.shape
    bl = img[:h // B * B, :w // B * B].reshape(h // B, B, w // B, B).mean(axis=(1, 3))
    sky = float(np.percentile(bl, 5))
    big = ndimage.gaussian_filter(bl, 8)
    cy, cx = np.unravel_index(int(np.argmax(big)), big.shape)
    # refine: flux-weighted centroid of the excess within 1.5x the first-guess radius
    yy, xx = np.indices(bl.shape)
    r = np.hypot(yy - cy, xx - cx).astype(int)
    prof = ndimage.mean(bl, r, range(r.max() + 1))
    cnt = np.bincount(r.ravel())
    prof = np.array(prof); ok = cnt[:len(prof)] >= 6
    pk = int(np.nanargmax(np.where(ok, prof, -1e9)))
    ring = float(prof[pk]); half = sky + 0.5 * (ring - sky)
    Rout = next((i for i in range(pk, len(prof)) if ok[i] and prof[i] < half), None)
    Rin = next((i for i in range(pk, -1, -1) if prof[i] < half), 0)
    return dict(cx=cx * B, cy=cy * B, ring=ring - sky, Rout=(Rout or 0) * B, Rin=Rin * B, prof=prof, sky=sky)


def save_glow(sm, fn):
    import cv2
    lo, hi = np.percentile(sm, 1), np.percentile(sm, 99.7)
    s = np.clip((sm - lo) / max(hi - lo, 1e-3), 0, 1)
    cv2.imwrite(fn, cv2.resize((s * 255).astype(np.uint8), (sm.shape[1] * 4, sm.shape[0] * 4), interpolation=cv2.INTER_NEAREST))


offs = [tuple(float(v) for v in o.split(",")) for o in a.offsets.split(";")]
eafs = [int(v) for v in a.eafs.split(",")] if a.eafs else [None]
m = Mount(host())
p = Pipes()
rows = []
try:
    p.setup("preview", a.exp, a.gain, 2)
    cur = None
    for (de, dn) in offs:
        if not a.no_goto and (de, dn) != cur:
            ra = (a.ra + de / 60.0 / 15.0 / math.cos(math.radians(a.dec))) % 24
            dec = a.dec + dn / 60.0
            log("goto offset E%+.1f' N%+.1f' -> RA %.5f Dec %.4f" % (de, dn, ra, dec))
            try:
                m.goto(ra, dec, wait=True, timeout=90)
            except Exception as ex:
                log("goto raised: %s" % ex)
            st = m.state(); log("   register RA %.5f Dec %.4f alt %.1f az %.1f pier %s track %s" % (st["RA"], st["Dec"], st["Alt"], st["Az"], st["pier_side"], st["is_enable_track"]))
            cur = (de, dn)
        for e in eafs:
            if e is not None:
                move_to(p, e)
            for k in range(a.frames):
                img, w, h, info = p.grab()
                pos = focuser_pos(p)
                o, sm = analyse(img)
                fn = os.path.join(a.outdir, "%s_%s_E%+.0f_N%+.0f_eaf%d_%d" % (time.strftime("%H%M%S"), a.tag, de, dn, pos, k))
                np.save(fn + ".npy", img.astype(np.uint16)); save_glow(sm, fn + "_glow.png")
                if o["edge"]:
                    cx, cy, R, ne, rs = o["edge"]
                    log("EAF %6d off E%+5.1f N%+5.1f: disc centre (%5.0f,%5.0f) R %5.0f px (diam %5.0f) edge pts %d resid %.0f | excess %.1f ADU frac %.2f sky %.0f noise %.2f -> %s" % (
                        pos, de, dn, cx, cy, R, 2 * R, ne, rs, o["excess"], o["frac"], o["sky"], o["noise"], os.path.basename(fn)))
                else:
                    log("EAF %6d off E%+5.1f N%+5.1f: NO EDGE  contrast %.1f ADU (noise %.2f) frac %.2f sky %.0f -> %s" % (
                        pos, de, dn, o["contrast"], o["noise"], o["frac"], o["sky"], os.path.basename(fn)))
                rr = radial(img)
                log("      radial: centre (%4.0f,%4.0f) ring %.1f ADU over sky  R_out %4.0f  R_in %4.0f  (diam %4.0f, hole %.2f)" % (
                    rr["cx"], rr["cy"], rr["ring"], rr["Rout"], rr["Rin"], 2 * rr["Rout"], rr["Rin"] / max(rr["Rout"], 1)))
                rows.append((pos, de, dn, o, rr))
finally:
    p.close(); m.close()
log("done: %d frames" % len(rows))
for pos, de, dn, o, rr in rows:
    log("SUMMARY EAF %6d  diam %5.0f  ring %6.1f ADU  hole %.2f" % (pos, 2 * rr["Rout"], rr["ring"], rr["Rin"] / max(rr["Rout"], 1)))
