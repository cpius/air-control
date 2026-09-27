#!/usr/bin/env python3
"""Focus scan on an extended target through VARIABLE CLOUD.

Contrast metrics (texture variance, edge width) fall with haze, so a cloud
passing looks like defocus. The SIZE of the defocus blur does not: out-of-focus
craters are rings whose diameter shrinks linearly toward focus whatever the
transparency. This measures that size as the half-max radius of the
autocorrelation of a band-passed frame -- a blur scale in pixels. Minimise it.

Positions are visited in an interleaved order and twice, so a slow change in
seeing or cloud cannot masquerade as a curve.

    python3 focus/focus_blurscale.py --pos 0,25000,50000,75000,100000 --rounds 2
"""
import argparse
import os
import sys
import time

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from session import Session
from focuscompare import move_to


def boxmean(img, k):
    p = np.pad(img, k // 2, mode="edge")
    return sliding_window_view(p, (k, k)).mean(axis=(2, 3))


def blur_scale(img, k1=3, k2=41, rmax=120):
    """Half-max radius (px) of the radially averaged autocorrelation of the
    band-passed central region. Returns (radius, snr) where snr is the ratio
    of the band-pass rms to the expected noise; low snr = untrustworthy."""
    h, w = img.shape
    c = img[h // 4: 3 * h // 4, w // 4: 3 * w // 4].astype(float)
    # The Air's frames keep the RGGB mosaic: the ACF of a raw frame oscillates
    # with a 2 px period (measured 1.00 0.59 0.82 0.34 0.60 ...). Fold each
    # 2x2 quad into one superpixel first; all scales below are in superpixels.
    hh, ww = (c.shape[0] // 2) * 2, (c.shape[1] // 2) * 2
    c = c[:hh, :ww].reshape(hh // 2, 2, ww // 2, 2).mean(axis=(1, 3))
    # 3x3 median next: a hot pixel that survives the fold still spikes the ACF.
    c = np.median(sliding_window_view(np.pad(c, 1, mode="edge"), (3, 3)), axis=(2, 3))
    med = np.median(c)
    mad = 1.4826 * np.median(np.abs(c - med))
    bp = boxmean(c, k1) - boxmean(c, k2)
    bp -= bp.mean()
    F = np.fft.rfft2(bp)
    acf = np.fft.irfft2(F * np.conj(F), s=bp.shape)
    acf = np.fft.fftshift(acf)
    cy, cx = np.array(acf.shape) // 2
    peak = acf[cy, cx]
    if peak <= 0:
        return None, 0.0
    rmax = int(min(rmax, cy - 1, cx - 1))
    Y, X = np.mgrid[-rmax:rmax + 1, -rmax:rmax + 1]
    R = np.hypot(X, Y).astype(int)
    sub = acf[cy - rmax: cy + rmax + 1, cx - rmax: cx + rmax + 1] / peak
    prof = np.array([sub[R == r].mean() for r in range(rmax + 1)])
    # noise-only ACF drops to ~0 by r=2 (the k1 box); a real blur keeps it high
    below = np.where(prof < 0.5)[0]
    r50 = float(below[0]) if len(below) else float(rmax)
    snr = float(bp.std() / max(mad / np.sqrt(k1 * k1), 1e-6))
    return r50, snr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--key", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "embedded_key.pem"))
    ap.add_argument("--pos", required=True, help="comma-separated focuser positions")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--frames", type=int, default=2)
    ap.add_argument("--exp", type=float, default=0.1)
    ap.add_argument("--gain", type=int, default=0)
    ap.add_argument("--bin", type=int, default=2)
    a = ap.parse_args()
    positions = [int(p) for p in a.pos.split(",")]
    s = Session(a.host, a.key, with_mount=False)
    try:
        s.c("open_focuser", [0])
        start = s.c("get_focuser_position")
        print("focuser at", start, flush=True)
        s.page("focus", a.exp, a.gain, binning=a.bin)
        time.sleep(a.exp + 1.5)
        acc = {p: [] for p in positions}
        # interleave: outer points first so a trend cannot look like a V
        order = sorted(positions, key=lambda p: abs(p - np.mean(positions)), reverse=True)
        for rnd in range(a.rounds):
            seq = order if rnd % 2 == 0 else order[::-1]
            for p in seq:
                move_to(s, p, timeout=120)
                s.fresh(1)
                vals = []
                for _ in range(a.frames):
                    v, w, h = s.fresh(1)
                    raw = np.frombuffer(v, dtype=np.uint8)
                    img = raw.reshape(h, w) if raw.size == w * h else raw.view(np.uint16).reshape(h, w)
                    r, snr = blur_scale(img)
                    if r is not None:
                        vals.append((r, snr, float(np.median(img))))
                if not vals:
                    print("  round %d %6d: no measurement" % (rnd, p), flush=True); continue
                r = float(np.median([x[0] for x in vals])); snr = float(np.median([x[1] for x in vals])); med = vals[0][2]
                acc[p].append(r)
                print("  round %d %6d: blur scale %5.1f px   snr %5.1f   median %6.0f" % (rnd, p, r, snr, med), flush=True)
        print()
        summary = []
        for p in positions:
            if acc[p]:
                summary.append((float(np.median(acc[p])), p))
                print("  %6d: blur %5.1f px  (%s)" % (p, np.median(acc[p]), " ".join("%.1f" % x for x in acc[p])))
        if summary:
            best = min(summary)
            move_to(s, best[1])
            print("best %d (blur %.1f px); focuser left there" % (best[1], best[0]))
        s.c("stop_exposure")
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
