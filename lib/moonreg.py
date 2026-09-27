#!/usr/bin/env python3
"""Sub-pixel registration of two heavily blurred lunar frames (or Bayer planes).

Phase correlation on raw pixels locks onto the sensor's fixed pattern (hot
pixels, PRNU, the Bayer screen) and reports zero shift for a field that is
plainly moving -- measured 2026-09-24. This helper band-passes first: 3x3
median (hot pixels), Gaussian smooth, subtract a wide Gaussian (illumination),
window, then plain cross-correlation with a quadratic fit to the peak.

    python3 lib/moonreg.py --selftest telemetry/2026-09-24/moonlook/213000_moon_preview_bin2_20.07ms_g100.npy
"""
import argparse, math, sys
import numpy as np
from scipy import ndimage


def bandpass(img, lo=2.0, hi=25.0):
    """Median-clean, smooth by `lo`, remove scales above `hi`, contrast-normalise, window."""
    a = ndimage.median_filter(np.asarray(img, dtype=np.float64), 3)
    a = ndimage.gaussian_filter(a, lo) - ndimage.gaussian_filter(a, hi)
    a /= ndimage.gaussian_filter(np.abs(a), 2 * hi) + 1e-9
    a -= a.mean()
    return a * np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))


def measure_shift(ref, mov, lo=2.0, hi=25.0, prepped=False):
    """Return (dx, dy, quality): mov's features sit at ref's position + (dx, dy)."""
    r = ref if prepped else bandpass(ref, lo, hi)
    m = mov if prepped else bandpass(mov, lo, hi)
    F = np.fft.fft2(r) * np.conj(np.fft.fft2(m))
    c = np.fft.ifft2(F).real
    c = np.fft.fftshift(c)
    cy0, cx0 = c.shape[0] // 2, c.shape[1] // 2
    py, px = np.unravel_index(int(np.argmax(c)), c.shape)
    # quadratic surface fit on a 5x5 neighbourhood of the peak
    y0, y1, x0, x1 = max(py - 2, 0), min(py + 3, c.shape[0]), max(px - 2, 0), min(px + 3, c.shape[1])
    win = c[y0:y1, x0:x1]
    Y, X = np.mgrid[y0:y1, x0:x1]
    A = np.column_stack([X.ravel() ** 2, Y.ravel() ** 2, X.ravel() * Y.ravel(), X.ravel(), Y.ravel(), np.ones(X.size)])
    coef, *_ = np.linalg.lstsq(A, win.ravel(), rcond=None)
    a2, b2, c2, d2, e2, _ = coef
    H = np.array([[2 * a2, c2], [c2, 2 * b2]])
    try:
        sx, sy = np.linalg.solve(H, [-d2, -e2])
    except np.linalg.LinAlgError:
        sx, sy = px, py
    if abs(sx - px) > 2 or abs(sy - py) > 2:
        sx, sy = px, py
    dx, dy = sx - cx0, sy - cy0
    q = float(c[py, px] / (np.sqrt((r * r).sum() * (m * m).sum()) + 1e-12))
    # convention check: ref(x) vs mov(x) = ref(x - d) gives a correlation peak at +d
    return -float(dx), -float(dy), q


def selftest(path, lo, hi):
    img = np.load(path).astype(np.float64)
    g = img[0::2, 1::2]
    rng = np.random.default_rng(3)
    print("self-test on %s green plane %s, lo %.1f hi %.1f" % (path, g.shape, lo, hi))
    worst = 0.0
    for tx, ty in [(3.0, -2.0), (0.4, 0.3), (-7.6, 5.2), (25.0, -12.5), (0.0, 0.0), (60.0, 33.0)]:
        mov = ndimage.shift(g, (ty, tx), order=3, mode="nearest") + rng.normal(0, 30, g.shape)
        dx, dy, q = measure_shift(g, mov, lo, hi)
        err = math.hypot(dx - tx, dy - ty); worst = max(worst, err)
        print("  true (%+6.2f, %+6.2f) -> measured (%+6.2f, %+6.2f)  err %.2f px  q %.3f" % (tx, ty, dx, dy, err, q))
    print("worst error %.2f px" % worst)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", required=True)
    ap.add_argument("--lo", type=float, default=2.0); ap.add_argument("--hi", type=float, default=25.0)
    a = ap.parse_args()
    selftest(a.selftest, a.lo, a.hi)
