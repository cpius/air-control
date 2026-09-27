#!/usr/bin/env python3
"""Where does the mount REALLY point? A rigid-rotation polar-misalignment model.

The mount's home position is mechanical, so with a polar axis that is off by
(eaz east, ealt up) the whole mount frame is the true frame rotated by that
much about the zenith and the east-west axis. A goto from a fresh home lands
at R * target. A sync at one point adds a constant (HA, Dec) offset in the
mount's own polar coordinates -- it zeroes the error there and leaves a
residual everywhere else that this model predicts.

Fit the two axis errors from measured offsets (solved - register, arcmin on
the sky), then predict the offset at any target for a given sync point:

    python3 pointing/pointing_model.py --lst 20.6132 \
        --meas 0.7454,8.9378,none,-242.2,30.1 \
        --predict -3.501,2.575,0.8510,8.9378

Angles in degrees, HA/RA in hours (HA = LST - RA, west positive).
"""
import argparse, math
import numpy as np

LAT = 55.689444


def _frame():
    phi = math.radians(LAT)
    P = np.array([0.0, math.cos(phi), math.sin(phi)])     # pole, ENU
    Q = np.array([0.0, -math.sin(phi), math.cos(phi)])    # equator on the meridian (south)
    W = np.array([-1.0, 0.0, 0.0])                         # west point
    return P, Q, W


def eqvec(ha_h, dec):
    P, Q, W = _frame()
    H, d = math.radians(ha_h * 15.0), math.radians(dec)
    return math.cos(d) * (math.cos(H) * Q + math.sin(H) * W) + math.sin(d) * P


def vec_eq(v):
    P, Q, W = _frame()
    dec = math.degrees(math.asin(max(-1.0, min(1.0, float(v @ P)))))
    ha = math.degrees(math.atan2(float(v @ W), float(v @ Q))) / 15.0
    return ha, dec


def Rmat(eaz, ealt):
    """eaz: axis east of north (deg, +east); ealt: axis above the true pole (deg, +up)."""
    a, b = math.radians(-eaz), math.radians(ealt)
    Rz = np.array([[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1]])
    Rx = np.array([[1, 0, 0], [0, math.cos(b), -math.sin(b)], [0, math.sin(b), math.cos(b)]])
    return Rz @ Rx


def pier(ha_h):
    """+1 west of the meridian (pier east), -1 east of it (pier west): cone and Dec-offset
    errors change sign across a meridian flip."""
    return 1.0 if ha_h >= 0 else -1.0


def tube(R, hm, dm, cone=0.0, deco=0.0):
    """Mount coordinates -> direction the tube points, with flip-aware cone (deg, in HA)
    and Dec-offset (deg) terms."""
    s = pier(hm)
    return R @ eqvec(hm + s * cone / 15.0 / math.cos(math.radians(dm)), dm + s * deco)


def pointed(R, ha, dec, sync=None, cone=0.0, deco=0.0):
    """Direction reached when the register is driven to (ha, dec); sync=(ha_s, dec_s) or None."""
    dh = dd = 0.0
    if sync is not None:
        for _ in range(8):     # fixed-point: the offsets that make the sync point exact
            h2, d2 = vec_eq(tube(R, sync[0] - dh, sync[1] - dd, cone, deco))
            dh += ((h2 - sync[0] + 12.0) % 24.0) - 12.0
            dd += d2 - sync[1]
    return tube(R, ha - dh, dec - dd, cone, deco)


def offset(R, ha, dec, sync=None, cone=0.0, deco=0.0):
    """(dRA, dDec) in arcmin on the sky: where the tube points MINUS the target."""
    h2, d2 = vec_eq(pointed(R, ha, dec, sync, cone, deco))
    dha = ((h2 - ha + 12.0) % 24.0) - 12.0
    return -dha * 15.0 * 60.0 * math.cos(math.radians(dec)), (d2 - dec) * 60.0


def fit(meas, x0=(0.0, 0.0), with_cone=False, with_deco=False):
    """meas: list of (ha, dec, sync|None, dra_arcmin, ddec_arcmin).
    Returns (eaz, ealt, cone, deco), residuals -- unused terms come back as 0."""
    from scipy.optimize import least_squares
    n = 2 + int(with_cone) + int(with_deco)

    def unpack(x):
        c = x[2] if with_cone else 0.0
        k = x[2 + int(with_cone)] if with_deco else 0.0
        return x[0], x[1], c, k

    def resid(x):
        eaz, ealt, c, k = unpack(x)
        R = Rmat(eaz, ealt)
        out = []
        for ha, dec, sync, dra, ddec in meas:
            m = offset(R, ha, dec, sync, c, k)
            out += [m[0] - dra, m[1] - ddec]
        return np.array(out)

    r = least_squares(resid, list(x0) + [0.0] * (n - 2))
    return unpack(r.x), resid(r.x)


def parse_sync(s):
    return None if s.lower() == "none" else tuple(float(v) for v in s.split(":"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--meas", action="append", required=True,
                    help="ha_h,dec,sync,dra_arcmin,ddec_arcmin  (sync = none or ha:dec of the sync point)")
    ap.add_argument("--predict", action="append", default=[],
                    help="ha_h,dec,sync  -> predicted offset (sync = none or ha:dec)")
    ap.add_argument("--cone", action="store_true", help="also fit a cone (non-perpendicularity) term, flips with pier side")
    ap.add_argument("--deco", action="store_true", help="also fit a Dec zero-point term, flips with pier side")
    a = ap.parse_args()
    meas = []
    for m in a.meas:
        ha, dec, sync, dra, ddec = m.split(",")
        meas.append((float(ha), float(dec), parse_sync(sync), float(dra), float(ddec)))
    (eaz, ealt, cone, deco), res = fit(meas, with_cone=a.cone, with_deco=a.deco)
    print("polar axis error: %.2f deg %s of north, %.2f deg %s ; cone %.2f deg ; dec offset %.2f deg ; residuals arcmin: %s" % (
        abs(eaz), "east" if eaz > 0 else "west", abs(ealt), "high" if ealt > 0 else "low", cone, deco, np.round(res, 1).tolist()))
    R = Rmat(eaz, ealt)
    for p in a.predict:
        ha, dec, sync = p.split(",")
        ha, dec, sync = float(ha), float(dec), parse_sync(sync)
        dra, ddec = offset(R, ha, dec, sync, cone, deco)
        print("at HA %+.3fh Dec %+.2f (sync %s): tube points %+.1f' RA %+.1f' Dec from the target -> command target %+.1f' RA %+.1f' Dec to centre it" % (
            ha, dec, sync, dra, ddec, -dra, -ddec))
