#!/usr/bin/env python3
"""Move the pointing by a requested amount in ALTITUDE and AZIMUTH, closed on the
mount's own Alt/Az readout -- for "the Moon is a bit up and to the right of the
tube": the person at the rig sees alt/az, not RA/Dec.

    ASIAIR_HOST=192.168.1.36 python3 -u pointing/altaznudge.py --up 2 --right 3      # degrees
    ASIAIR_HOST=192.168.1.36 python3 -u pointing/altaznudge.py --down 0.5 --left 1

"right" = increasing azimuth (toward the south when facing east/south-east).
The response of the two axes to 'north' and 'east' pulses is measured with two
short nudges (a 2x2 matrix in deg/s), then the move is solved and executed in
chunks with re-reads until within --tol. Guards: --min-alt, chunks <= --chunk s.
"""
import argparse, math, os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from joystick import Joystick

ap = argparse.ArgumentParser()
ap.add_argument("--up", type=float, default=0.0); ap.add_argument("--down", type=float, default=0.0)
ap.add_argument("--right", type=float, default=0.0); ap.add_argument("--left", type=float, default=0.0)
ap.add_argument("--rate", type=int, default=5, help="slew-rate index (5=60x ~0.29 deg/s, 6=MAX/2 ~3 deg/s)")
ap.add_argument("--chunk", type=float, default=3.0)
ap.add_argument("--tol", type=float, default=0.08, help="degrees")
ap.add_argument("--min-alt", type=float, default=7.0)
a = ap.parse_args()

def log(s): print("%s %s" % (time.strftime("%H:%M:%S"), s), flush=True)

j = Joystick(os.environ["ASIAIR_HOST"])
def altaz():
    s = j.state(); return np.array([s["Alt"], s["Az"]]), s

def daz(a2, a1):
    return (a2 - a1 + 180) % 360 - 180

try:
    j.set_rate(a.rate); time.sleep(0.2)
    p0, s = altaz()
    target = np.array([p0[0] + a.up - a.down, (p0[1] + a.right - a.left) % 360])
    log("from alt %.2f az %.2f (RA %.4f Dec %+.3f pier %s) -> target alt %.2f az %.2f" % (p0[0], p0[1], s["RA"], s["Dec"], s["pier_side"], target[0], target[1]))
    if target[0] < a.min_alt:
        raise SystemExit("target altitude %.2f is below --min-alt %.1f" % (target[0], a.min_alt))
    # response matrix: columns = effect (dAlt, dAz) per second of 'north' and 'east'
    M = np.zeros((2, 2))
    for k, d in enumerate(("north", "east")):
        b, _ = altaz(); j.nudge(d, 0.6); c, _ = altaz()
        M[:, k] = [(c[0] - b[0]) / 0.6, daz(c[1], b[1]) / 0.6]
        log("  '%s' 0.6s: dAlt %+.3f dAz %+.3f -> %.3f / %.3f deg/s" % (d, c[0] - b[0], daz(c[1], b[1]), M[0, k], M[1, k]))
    if abs(np.linalg.det(M)) < 1e-4:
        raise SystemExit("axes are degenerate here (det %.2e) -- move elsewhere first" % np.linalg.det(M))
    for it in range(8):
        p, s = altaz()
        err = np.array([target[0] - p[0], daz(target[1], p[1])])
        log("iter %d: alt %.2f az %.2f  err alt %+.2f az %+.2f" % (it, p[0], p[1], err[0], err[1]))
        if np.hypot(*err) < a.tol:
            break
        secs = np.linalg.solve(M, err) * 0.9          # seconds of 'north', 'east' (negative = opposite)
        for k, d in enumerate(("north", "east")):
            t = float(secs[k])
            if abs(t) < 0.15:
                continue
            dirn = d if t > 0 else {"north": "south", "east": "west"}[d]
            remaining = abs(t)
            while remaining > 0.05:
                dt = min(a.chunk, remaining)
                j.nudge(dirn, dt); remaining -= dt
                p, s = altaz()
                if p[0] < a.min_alt:
                    raise SystemExit("altitude %.2f below --min-alt -- stopped" % p[0])
    p, s = altaz()
    log("END alt %.2f az %.2f (RA %.4f Dec %+.3f)  moved %+.2f up, %+.2f right of the start" % (p[0], p[1], s["RA"], s["Dec"], p[0] - p0[0], daz(p[1], p0[1])))
finally:
    j.close()
