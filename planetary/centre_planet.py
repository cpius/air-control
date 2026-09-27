#!/usr/bin/env python3
"""Put a bright planet on the sensor centre, by measuring where it is and
moving the mount by that much -- not by plate solving (a first-magnitude
planet in the field makes the solver grind) and not by `start_auto_goto`.

Frame-to-sky transform: measured 2026-09-02 from Titan/Rhea/Iapetus against
Horizons (preview page, full res): 2.113 px/arcsec, rotation 164.9 deg,
mirrored. If the camera is rotated, re-fit it (see saturn-moons memory).

    python3 centre_planet.py --host <air-ip> --goto 0.90161 2.90708
"""
import argparse
import math
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session import Session

S_PX_PER_ARCSEC = 2.113
THETA = math.radians(164.9)


def px_to_sky(px, py):
    """image offset (right, down) -> (E, N) arcsec, using the fitted transform."""
    c, s = math.cos(THETA), math.sin(THETA)
    E = (px * c - py * s) / S_PX_PER_ARCSEC
    n = (-px * s - py * c) / S_PX_PER_ARCSEC
    return E, -n


def planet_centroid(v, w, h):
    img = np.asarray(v, dtype=np.int32).reshape(h, w)
    thr = max(60000, int(img.max() * 0.5))
    ys, xs = np.nonzero(img >= thr)
    if len(xs) < 20:
        return None, len(xs)
    mx, my = np.median(xs), np.median(ys)
    m = (np.abs(xs - mx) < 120) & (np.abs(ys - my) < 120)
    return (float(xs[m].mean()), float(ys[m].mean())), int(m.sum())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--key", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "embedded_key.pem"))
    ap.add_argument("--goto", nargs=2, type=float, metavar=("RA_H", "DEC_D"), help="JNow/apparent")
    ap.add_argument("--exp", type=float, default=0.1)
    ap.add_argument("--gain", type=int, default=250)
    ap.add_argument("--tol", type=float, default=25, help="px")
    ap.add_argument("--tries", type=int, default=4)
    a = ap.parse_args()
    s = Session(a.host, a.key, with_mount=True)
    try:
        m = s.mount()
        if a.goto:
            print("goto RA %.5fh Dec %+.4f" % tuple(a.goto), flush=True)
            m.goto(a.goto[0], a.goto[1]); time.sleep(2)
        s.page("preview", a.exp, a.gain, binning=1); time.sleep(a.exp + 1)
        for i in range(a.tries):
            v, w, h = s.fresh(2)
            c, n = planet_centroid(v, w, h)
            if c is None:
                print("try %d: no saturated blob in frame (%d px over threshold)" % (i + 1, n), flush=True)
                return 1
            px, py = c[0] - w / 2, c[1] - h / 2
            E, N = px_to_sky(px, py)
            st = m.state()
            print("try %d: planet at (%.0f,%.0f) = %+.0f,%+.0f px from centre -> %+.0f\" E %+.0f\" N  (%d sat px)" % (
                i + 1, c[0], c[1], px, py, E, N, n), flush=True)
            if math.hypot(px, py) <= a.tol:
                print("centred to %.0f px (%.0f\")" % (math.hypot(px, py), math.hypot(px, py) / S_PX_PER_ARCSEC))
                break
            ra = st["RA"] + E / (3600 * 15 * math.cos(math.radians(st["Dec"])))
            dec = st["Dec"] + N / 3600
            print("   moving to RA %.5fh Dec %+.4f" % (ra, dec), flush=True)
            s.c("stop_exposure"); time.sleep(0.5)
            m.goto(ra, dec); time.sleep(2)
            s.c("start_exposure"); time.sleep(a.exp + 1)
        s.c("stop_exposure")
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
