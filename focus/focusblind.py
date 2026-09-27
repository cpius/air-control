#!/usr/bin/env python3
"""Find focus without needing to identify -- or even have -- a named star.

The rig's pointing frame is unreliable after the mount was moved, so no
particular star can be put in the field on demand. But focus does not need one.
At gross defocus a star's flux is smeared over a ~300 px disc and almost
nothing clears the noise; as focus improves the same flux concentrates and
stars cross the detection threshold in numbers. So the metric is simply HOW
MANY sources are detectable, maximised over focuser position. It works on any
field, needs no astrometry, and cannot be fooled by a hot pixel -- the two
known ones are excluded by position, and hot pixels do not multiply.

Once the count peaks, the field is sharp enough to plate-solve, and the solver
tolerates a register error of ~12 deg, which then fixes the pointing too.
"""
import argparse, sys, time
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import numpy as np
from fastgrab import Grabber
from daypipes import log, boxmean

HOT = [(1919, 336), (125, 48)]        # measured, stable, bin2 coordinates

def count_sources(img, nsig=8.0, sep=40):
    sm = boxmean(img, 5)
    bg = float(np.median(sm))
    sig = max(0.3, 1.4826 * float(np.median(np.abs(sm[::5, ::5] - bg))))
    mask = sm > bg + nsig * sig
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return 0, bg, sig, []
    order = np.argsort(sm[ys, xs])[::-1]
    pts = []
    for i in order[:60000]:
        x, y = int(xs[i]), int(ys[i])
        if any((x - a) ** 2 + (y - b) ** 2 < 900 for a, b in HOT):
            continue
        if any((x - p[0]) ** 2 + (y - p[1]) ** 2 < sep * sep for p in pts):
            continue
        pts.append((x, y))
        if len(pts) >= 300:
            break
    return len(pts), bg, sig, pts

def move_to(g, pos, timeout=180):
    s = g.s
    start = s.c("get_focuser_position")
    n = abs(int(pos) - int(start))
    if n == 0:
        return
    log("  move focuser %s -> %d (%d steps)" % (start, pos, n))
    s.c("move_focuser", [int(pos)])
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.8)
        st = s.c("get_focuser_state")
        if isinstance(st, dict) and st.get("state") == "idle":
            return
    log("  MOVE TIMED OUT")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pos", required=True, help="comma-separated focuser positions")
    ap.add_argument("--exp", type=float, default=4.0)
    ap.add_argument("--gain", type=int, default=300)
    ap.add_argument("--sigma", type=float, default=8.0)
    ap.add_argument("--goto-best", action="store_true")
    a = ap.parse_args()
    positions = [int(x) for x in a.pos.split(",")]
    g = Grabber(exp=a.exp, gain=a.gain, binning=2)
    res = []
    try:
        for p in positions:
            move_to(g, p)
            t = time.time()
            img, w, h = g.frame()
            n, bg, sig, pts = count_sources(img, nsig=a.sigma)
            log("pos %6d   %3d sources   bg=%.0f sigma=%.1f max=%.0f   (%.1fs)"
                % (p, n, bg, sig, img.max(), time.time() - t))
            res.append((p, n))
        log("=== summary: sources detected vs focuser position ===")
        for p, n in res:
            log("   %6d   %3d  %s" % (p, n, "#" * min(n, 60)))
        if res:
            best = max(res, key=lambda t: t[1])
            log("most sources: %d at position %d" % (best[1], best[0]))
            if a.goto_best and best[1] > 0:
                move_to(g, best[0]); log("parked at %d" % best[0])
    finally:
        g.close(); log("closed")
