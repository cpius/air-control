#!/usr/bin/env python3
"""Polar-alignment drift test: plate-solve the same field for N minutes and
fit the Dec drift. Pointing check comes free: every row also prints the
mount register next to the solved position.

    python3 drift.py --host <air-ip> --minutes 6            # where it points now
    python3 drift.py --host <air-ip> --goto 23.4 28 --minutes 6

Geometry (from the 2026-08-25 lesson): the Dec drift rate measures ONE
component of the polar error, chosen by hour angle —
  near the meridian (cos HA ~ 1)  -> the AZIMUTH error
  near due east/west (sin HA ~ 1) -> the ALTITUDE error
Measure far from the pole (cos Dec near 1). 0.262"/min of Dec drift per
arcminute of polar error (15.04"/s * sin 1').

The first solve after a slew is dropped from the fit: it sits a few arcsec off
the rest while the mount settles and doubles the fitted rate on its own.
"""
import argparse
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skysurvey as ss
from mount import Mount

K = 15.041 * 60 * math.sin(math.radians(1 / 60.0))   # "/min per arcmin of PA error


def fit(ts, ys):
    n = len(ts)
    if n < 2:
        return float("nan"), float("nan")
    mt, my = sum(ts) / n, sum(ys) / n
    sxx = sum((t - mt) ** 2 for t in ts)
    sxy = sum((t - mt) * (y - my) for t, y in zip(ts, ys))
    b = sxy / sxx
    if n < 3:
        return b, float("nan")
    res = sum((y - (my + b * (t - mt))) ** 2 for t, y in zip(ts, ys))
    se = math.sqrt(res / (n - 2) / sxx)
    return b, se


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"))
    ap.add_argument("--key", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "embedded_key.pem"))
    ap.add_argument("--minutes", type=float, default=6.0)
    ap.add_argument("--exp", type=float, default=3.0)
    ap.add_argument("--gain", type=int, default=250)
    ap.add_argument("--goto", nargs=2, type=float, metavar=("RA_H", "DEC_D"))
    ap.add_argument("--interval", type=float, default=30.0, help="seconds between solves")
    a = ap.parse_args()
    ss.HOST, ss.KEY = a.host, a.key

    rig = ss.Rig()
    st = rig.ensure_tracking()
    if a.goto:
        m = Mount(a.host)
        print("goto RA %.3fh Dec %+.2f ..." % tuple(a.goto), flush=True)
        m.goto(a.goto[0], a.goto[1])
        time.sleep(3)
    rig.cam("stop_exposure")
    time.sleep(1)
    rig.cam("set_page", ["preview"])
    rig.cam("set_camera_bin", [1])
    rig.cam("set_control_value", ["Gain", a.gain])

    rows = []
    t_end = time.time() + a.minutes * 60
    print("  t(min)   solvedRA(h)   solvedDec   stars  regRA(h)  regDec   dRA(')  dDec(')  alt   az    HA(h)", flush=True)
    while True:
        t0 = time.time()
        ok, _ = ss.expose(rig, a.exp)
        if not ok:
            print("  exposure failed", flush=True)
            time.sleep(5)
            if time.time() > t_end:
                break
            continue
        r, dt, state = ss.plate_solve(rig, cap=75)
        reg = rig.mnt("scope_get_ra_dec")["result"]
        alt, az = rig.horiz()
        if r:
            ra, dec = r["ra_dec"]
            lst = reg[2] if len(reg) > 2 else float("nan")
            ha = ((lst - ra + 12) % 24) - 12
            dra = (reg[0] - ra) * 15 * 60 * math.cos(math.radians(dec))
            ddec = (reg[1] - dec) * 60
            rows.append((time.time(), ra, dec, r.get("star_number"), ha))
            print("  %6.2f   %10.5f   %+9.5f   %5s  %8.4f  %+7.3f  %6.2f  %6.2f  %4.1f  %5.1f  %+5.2f" % (
                (rows[-1][0] - rows[0][0]) / 60, ra, dec, r.get("star_number"),
                reg[0], reg[1], dra, ddec, alt, az, ha), flush=True)
        else:
            print("  no solve (%s, %.0fs)  reg RA %.4f Dec %+.3f  alt %.1f az %.1f" % (
                state, dt, reg[0], reg[1], alt, az), flush=True)
        if time.time() > t_end:
            break
        time.sleep(max(0, a.interval - (time.time() - t0)))

    use = rows[1:] if len(rows) > 3 else rows
    if len(use) < 2:
        print("not enough solves to fit"); return 1
    ts = [(r[0] - use[0][0]) / 60 for r in use]
    mdec = sum(r[2] for r in use) / len(use)
    bdec, sdec = fit(ts, [(r[2] - mdec) * 3600 for r in use])
    mra = sum(r[1] for r in use) / len(use)
    bra, sra = fit(ts, [(r[1] - mra) * 3600 * 15 * math.cos(math.radians(mdec)) for r in use])
    ha = sum(r[4] for r in use) / len(use)
    print()
    print("fit over %d solves, %.1f min, mean HA %+.2fh, Dec %+.1f (first solve dropped: %s)" % (
        len(use), ts[-1], ha, mdec, len(use) < len(rows)))
    print("  Dec drift  %+6.2f +/- %.2f \"/min" % (bdec, sdec))
    print("  RA  drift  %+6.2f +/- %.2f \"/min  (tracking-rate term, not PA)" % (bra, sra))
    comp = "AZIMUTH" if abs(math.cos(math.radians(ha * 15))) > abs(math.sin(math.radians(ha * 15))) else "ALTITUDE"
    w = max(abs(math.cos(math.radians(ha * 15))), abs(math.sin(math.radians(ha * 15))))
    err = abs(bdec) / (K * w)
    print("  -> polar error, %s component: ~%.1f' (+/- %.1f')   [geometry weight %.2f]" % (
        comp, err, sdec / (K * w), w))
    return 0


if __name__ == "__main__":
    sys.exit(main())
