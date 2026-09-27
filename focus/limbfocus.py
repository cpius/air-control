#!/usr/bin/env python3
"""Focus on the Moon's limb: sweep the EAF and measure the limb's edge width.

    ASIAIR_HOST=192.168.1.36 python3 -u limbfocus.py --pos 55000,60000,65000 --frames 2 --exp-ms 10 --gain 100 --limits 30000,70000

Metric: on the green superpixels of a preview frame (bin 2, 16-bit RGGB), each
row (or column) that starts in sky is scanned from the sky end to the first
crossing of the half-contrast level -- that crossing is the limb, never a mare
boundary inside the disc. width = contrast / |grad I| at the crossing, i.e. the
edge's 0-100% width along its own normal (2.5 sigma for a Gaussian blur, 0.53 D
for an out-of-focus annulus with the C8's 35% obstruction). The arms of the V
are linear in EAF steps far from focus, so two positions on one arm plus one
on the other locate the vertex.

--scale is the bin-1 arcsec/px used only to print widths in arcsec.
Fine focus: --page focus --bin 1 (1:1 crop, 16-bit RGGB) once the limb is near the centre.
"""
import argparse, json, math, os, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daypipes
from daypipes import host, Pipes, log, save_png, focuser_pos, move_to

ap = argparse.ArgumentParser()
ap.add_argument("--pos", required=True, help="comma list of EAF positions")
ap.add_argument("--rounds", type=int, default=1)
ap.add_argument("--order", default="interleaved", choices=["interleaved", "linear"])
ap.add_argument("--frames", type=int, default=2, help="frames per position (after one discarded)")
ap.add_argument("--page", default="preview", choices=["preview", "focus"], help="focus = 1:1 centre crop, ~1.6 s/frame")
ap.add_argument("--bin", type=int, default=2)
ap.add_argument("--exp-ms", type=float, default=10.0); ap.add_argument("--gain", type=int, default=100)
ap.add_argument("--auto-exp", action="store_true", help="set exposure once so the disc plateau sits at 25000-45000 ADU")
ap.add_argument("--scale", type=float, default=0.135, help="bin-1 arcsec/px, for printing only")
ap.add_argument("--limits", default="30000,70000", help="EAF LO,HI allowed")
ap.add_argument("--goto-best", action="store_true", help="finish at the fitted vertex (else at the best sample)")
ap.add_argument("--park", type=int, default=None, help="finish at this EAF position instead")
ap.add_argument("--tag", default="limbfocus")
ap.add_argument("--outdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "telemetry", time.strftime("%Y-%m-%d"), "limbfocus"))
a = ap.parse_args()
daypipes.LO, daypipes.HI = [int(v) for v in a.limits.split(",")]
os.makedirs(a.outdir, exist_ok=True)
BIAS = 3900.0
SP_ARCSEC = 2 * a.bin * a.scale           # one green superpixel = 2 x bin sensor px


def green(img):
    img = img.astype(np.float32) - BIAS
    return 0.5 * (img[0::2, 1::2] + img[1::2, 0::2])       # RGGB: G at (0,1) and (1,0)


def limb_width(img):
    g = ndimage.gaussian_filter(green(img), 1.0)
    sky = float(np.percentile(g, 2)); plat = float(np.percentile(g, 97))
    C = plat - sky
    if C < 2000:
        return None
    thr = sky + 0.5 * C; skylim = sky + 0.1 * C
    gy, gx = np.gradient(g)
    gm = np.hypot(gx, gy)
    widths, xs, ys = [], [], []
    H, W = g.shape
    for axis in (0, 1):                                   # rows, then columns
        lines = g if axis == 0 else g.T
        gml = gm if axis == 0 else gm.T
        for i, prof in enumerate(lines):
            for rev in (False, True):
                pr = prof[::-1] if rev else prof
                if pr[0] > skylim or pr[1] > skylim:
                    continue
                k = np.argmax(pr > thr)
                if k == 0 or pr[k] <= thr or k > len(pr) - 3:
                    continue
                j = len(pr) - 1 - k if rev else k
                grad = gml[i, j]
                if grad <= 0:
                    continue
                widths.append(C / grad)
                if axis == 0: xs.append(j); ys.append(i)
                else: xs.append(i); ys.append(j)
    if len(widths) < 20:
        return None
    w = np.array(widths)
    return dict(width_sp=float(np.median(w)), width_p25=float(np.percentile(w, 25)), n=len(w), contrast=C, sky=sky,
                limb_x=float(np.median(xs)) * 2, limb_y=float(np.median(ys)) * 2)   # limb centroid in frame px


positions = [int(v) for v in a.pos.split(",")]
seq = []
for r in range(a.rounds):
    ps = list(positions)
    if a.order == "interleaved" and r % 2 == 1:
        ps = ps[::-1]
    seq += [(r, p) for p in ps]

p = Pipes()
t0 = time.time()
rec = []
try:
    start = focuser_pos(p)
    log("EAF start %d; %d positions x %d rounds, %d frames each" % (start, len(positions), a.rounds, a.frames))
    exp = a.exp_ms / 1000.0
    p.setup(a.page, exp, a.gain, a.bin)
    if a.auto_exp:
        for it in range(5):
            img, *_ = p.grab(timeout=40)
            plat = float(np.percentile(green(img), 97))
            log("  auto %d: %.2f ms -> plateau %.0f" % (it, exp * 1000, plat))
            if 25000 <= plat <= 45000:
                break
            exp = float(np.clip(exp * 35000.0 / max(plat, 200.0), 0.0002, 0.5))
            p.setup(a.page, exp, a.gain, a.bin)
    for r, pos in seq:
        got = move_to(p, pos)
        p.grab(timeout=40)                                # discard: may have started before the move ended
        ws = []
        for f in range(a.frames):
            img, w, h, info = p.grab(timeout=40)
            m = limb_width(img)
            if m is None:
                log("  [%5.0fs] r%d EAF %d f%d: NO LIMB (contrast too low or no sky edge)" % (time.time() - t0, r, got, f)); continue
            ws.append(m["width_sp"])
            fn = save_png(img, os.path.join(a.outdir, "%s_%s_r%d_%d_f%d.png" % (time.strftime("%H%M%S"), a.tag, r, got, f)), shrink=2) if f == 0 else ""
            rec.append(dict(t=time.time() - t0, round=r, eaf=got, frame=f, exp_ms=exp * 1000, **m))
            log("  [%5.0fs] r%d EAF %6d f%d: limb width %6.2f sp = %6.1f\"  (p25 %.2f, n %d)  contrast %.0f  limb at (%.0f,%.0f) px" %
                (time.time() - t0, r, got, f, m["width_sp"], m["width_sp"] * SP_ARCSEC, m["width_p25"], m["n"], m["contrast"], m["limb_x"], m["limb_y"]))
        if ws:
            log("  EAF %6d: mean width %.2f sp = %.1f\"" % (got, np.mean(ws), np.mean(ws) * SP_ARCSEC))
    # summary + V fit
    byp = {}
    for x in rec:
        byp.setdefault(x["eaf"], []).append(x["width_sp"])
    xs = np.array(sorted(byp)); ys = np.array([np.mean(byp[k]) for k in xs])
    log("SUMMARY (EAF: width sp / arcsec):")
    for x, y in zip(xs, ys):
        log("  %6d  %7.2f  %7.1f\"" % (x, y, y * SP_ARCSEC))
    best = int(xs[np.argmin(ys)])
    vertex = None
    if len(xs) >= 3:
        i = int(np.argmin(ys))
        if 0 < i < len(xs) - 1:
            c = np.polyfit(xs[i - 1:i + 2], ys[i - 1:i + 2], 2)
            if c[0] > 0:
                vertex = int(round(-c[1] / (2 * c[0])))
                log("parabola through the 3 best: vertex %d" % vertex)
        else:
            log("minimum at the END of the sweep (%d) -- focus lies beyond it" % best)
    json.dump(dict(rec=rec, best=best, vertex=vertex), open(os.path.join(a.outdir, "%s_%s.json" % (time.strftime("%H%M%S"), a.tag)), "w"), indent=1)
    final = a.park if a.park is not None else (vertex if (a.goto_best and vertex) else best)
    move_to(p, final)
    log("EAF left at %d (%.0fs total)" % (focuser_pos(p), time.time() - t0))
finally:
    try: p.close()
    except Exception: pass
