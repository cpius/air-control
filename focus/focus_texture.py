#!/usr/bin/env python3
"""Focus scan on an extended target (the lunar surface) using a texture metric
that does not care where the limb is or how the target drifts: the variance of
a band-pass filtered frame, normalised by its mean brightness. Out-of-focus
craters are soft rings, so band-pass power at a few-pixel scale rises steadily
toward focus and is capped only by seeing.

    python3 focus/focus_texture.py --lo 20000 --hi 100000 --step 8000 --exp 0.1
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


def texture(img, k1=3, k2=15):
    """normalised band-pass variance in the central half of the frame, hot pixels clipped."""
    h, w = img.shape
    c = img[h // 4: 3 * h // 4, w // 4: 3 * w // 4].astype(float)
    med = np.median(c)
    c = np.minimum(c, med + 6 * 1.4826 * np.median(np.abs(c - med)) + 3000)   # clip hot pixels
    bp = boxmean(c, k1) - boxmean(c, k2)
    return float(bp.std() / max(c.mean(), 1.0)) * 1000, float(c.mean()), float(c.max())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--key", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "embedded_key.pem"))
    ap.add_argument("--lo", type=int, required=True)
    ap.add_argument("--hi", type=int, required=True)
    ap.add_argument("--step", type=int, default=8000)
    ap.add_argument("--exp", type=float, default=0.1)
    ap.add_argument("--gain", type=int, default=0)
    ap.add_argument("--bin", type=int, default=2)
    ap.add_argument("--frames", type=int, default=2)
    a = ap.parse_args()
    s = Session(a.host, a.key, with_mount=False)
    try:
        s.c("open_focuser", [0])
        print("focuser at", s.c("get_focuser_position"), flush=True)
        s.page("focus", a.exp, a.gain, binning=a.bin)
        time.sleep(a.exp + 1.5)
        hist = []
        for p in range(a.lo, a.hi + 1, a.step):
            move_to(s, p, timeout=120)
            s.fresh(1)
            vals = []
            for _ in range(a.frames):
                v, w, h = s.fresh(1)
                raw = np.frombuffer(v, dtype=np.uint8)
                img = raw.reshape(h, w) if raw.size == w * h else raw.view(np.uint16).reshape(h, w)
                vals.append(texture(img))
            t = float(np.median([x[0] for x in vals])); mean = vals[0][1]; mx = vals[0][2]
            hist.append((p, t))
            print("  %6d: texture %7.2f   mean %6.0f  max %6.0f" % (p, t, mean, mx), flush=True)
        best = max(hist, key=lambda x: x[1])
        move_to(s, best[0])
        s.c("stop_exposure")
        print("scan done; best %d (texture %.2f); focuser at %s" % (best[0], best[1], s.c("get_focuser_position")), flush=True)
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
