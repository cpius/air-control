#!/usr/bin/env python3
"""Find focus on a planet from far out of focus, without the autofocuser.

A defocused planet is a disc whose diameter grows linearly with distance from
focus: D = k * |p - p*| + D0. Three positions give the V; jump to its apex,
halve the step, repeat. The last stage is an interleaved A/B/C on the PEAK
(a sharper planet is a taller planet), because near focus the diameter
bottoms out at the planet's own size and stops discriminating.

Frames come from the focus page at bin 2 (14.4' x 8.1' at 2032 mm) so a big
donut still fits, then bin 1 for the fine stage.

    python3 focus/focus_planet.py --host <air-ip> --step 5000
"""
import argparse
import math
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from session import Session
from focuscompare import move_to


def frame(s):
    v, w, h = s.fresh(1)
    raw = np.frombuffer(v, dtype=np.uint8)
    img = raw.reshape(h, w) if raw.size == w * h else raw.view(np.uint16).reshape(h, w)
    return img.astype(np.int32), w, h


def measure(img, w, h, nsig=8):
    """(diameter px, peak above bg, centroid) of the brightest extended thing."""
    bg = int(np.median(img[::7, ::7]))
    sig = max(1.0, 1.4826 * float(np.median(np.abs(img[::7, ::7] - bg))))
    ys, xs = np.nonzero(img > bg + nsig * sig)
    if len(xs) < 30:
        return None
    mx, my = np.median(xs), np.median(ys)
    m = (np.abs(xs - mx) < 500) & (np.abs(ys - my) < 500)
    xs, ys = xs[m], ys[m]
    # equivalent diameter from area is robust to a donut hole; extent catches a ring
    d_area = 2 * math.sqrt(len(xs) / math.pi)
    d_ext = max(xs.max() - xs.min(), ys.max() - ys.min())
    return {"d": max(d_area, 0.8 * d_ext), "peak": int(img[ys, xs].max()) - bg,
            "cx": float(xs.mean()), "cy": float(ys.mean()), "n": len(xs), "bg": bg, "sig": sig}


def sample(s, pos, k=2):
    move_to(s, pos)
    s.fresh(1)                 # frame exposed during the move
    ms = []
    for _ in range(k):
        img, w, h = frame(s)
        m = measure(img, w, h)
        if m:
            ms.append(m)
    if not ms:
        return None
    ms.sort(key=lambda m: m["d"])
    return ms[len(ms) // 2]


def vertex(pts):
    """pts: [(pos, d)]. Fit d = k|p - p*| + d0 by scanning p* between samples."""
    best = None
    lo, hi = min(p for p, _ in pts), max(p for p, _ in pts)
    for pstar in np.linspace(lo - (hi - lo), hi + (hi - lo), 801):
        X = np.array([abs(p - pstar) for p, _ in pts]); Y = np.array([d for _, d in pts])
        A = np.vstack([X, np.ones_like(X)]).T
        (k, d0), res, *_ = np.linalg.lstsq(A, Y, rcond=None)
        if k <= 0:
            continue
        r = float(((A @ np.array([k, d0]) - Y) ** 2).sum())
        if best is None or r < best[0]:
            best = (r, pstar, k, d0)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--key", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "embedded_key.pem"))
    ap.add_argument("--step", type=int, default=5000, help="initial +/- step")
    ap.add_argument("--exp", type=float, default=0.3)
    ap.add_argument("--gain", type=int, default=100)
    ap.add_argument("--min-step", type=int, default=300)
    ap.add_argument("--fine", type=int, default=150, help="final interleaved A/B/C half-spacing")
    ap.add_argument("--lo", type=int, default=0)
    ap.add_argument("--hi", type=int, default=600000)
    a = ap.parse_args()
    s = Session(a.host, a.key, with_mount=False)
    try:
        s.c("open_focuser", [0])
        p0 = int(s.c("get_focuser_position"))
        print("start %d" % p0, flush=True)
        s.page("focus", a.exp, a.gain, binning=2); time.sleep(a.exp + 1)
        step = a.step
        centre = p0
        hist = []
        while step >= a.min_step:
            pts = []
            for p in (centre - step, centre, centre + step):
                p = max(a.lo, min(a.hi, p))
                m = sample(s, p)
                if m is None:
                    print("  %7d: nothing found" % p, flush=True); continue
                pts.append((p, m["d"]))
                hist.append((p, m["d"], m["peak"]))
                print("  %7d: D %6.1f px  peak %6d  n %6d  at (%.0f,%.0f)" % (p, m["d"], m["peak"], m["n"], m["cx"], m["cy"]), flush=True)
            if len(pts) < 2:
                print("lost the planet -- stopping at %d" % centre); move_to(s, centre); return 1
            if len(pts) == 3 and pts[1][1] <= pts[0][1] and pts[1][1] <= pts[2][1]:
                # minimum is inside: fit the V from all history near here and tighten
                fit = vertex([(p, d) for p, d, _ in hist if abs(p - centre) <= 2 * step])
                if fit:
                    centre = int(round(fit[1]))
                    print("V apex %d (k %.3f px/step, floor %.0f px)" % (centre, fit[2], fit[3]), flush=True)
                step //= 2
            else:
                # slope: walk downhill, growing the step until the minimum is bracketed
                lo_p, lo_d = min(pts, key=lambda t: t[1])
                fit = vertex([(p, d) for p, d, _ in hist[-6:]])
                if fit and abs(fit[1] - centre) < 6 * step:
                    centre = int(round(fit[1]))
                    print("V apex (extrapolated) %d" % centre, flush=True)
                else:
                    centre = lo_p + (step if lo_p > centre else -step)
                    print("downhill toward %d" % centre, flush=True)
            centre = max(a.lo, min(a.hi, centre))
        # fine stage: interleaved A/B/C on the peak at bin 1
        s.page("focus", a.exp, a.gain, binning=1); time.sleep(a.exp + 1)
        cand = [centre - a.fine, centre, centre + a.fine]
        acc = {p: [] for p in cand}
        for rnd in range(2):
            for p in (cand if rnd % 2 == 0 else cand[::-1]):
                m = sample(s, p, k=3)
                if m:
                    acc[p].append((m["peak"], m["d"]))
                    print("  fine %d: peak %6d  D %6.1f" % (p, m["peak"], m["d"]), flush=True)
        score = {p: (np.median([pk for pk, _ in v]) if v else -1) for p, v in acc.items()}
        win = max(score, key=score.get)
        move_to(s, win)
        s.c("stop_exposure")
        print("focuser left at %d (peaks: %s)" % (win, {p: int(v) for p, v in score.items()}))
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
