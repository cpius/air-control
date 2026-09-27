#!/usr/bin/env python3
"""Single-frame sharpness for a blurred extended scene (lunar surface), robust
to the scene sliding between exposures: radially averaged power spectrum of
the green plane, noise floor taken from the highest spatial frequencies (the
optics pass nothing there), signal = noise-corrected power in a mid band,
normalised by the low-frequency (scene contrast) power. Also reports the
frequency where the corrected spectrum falls to 10% of its low-band level --
a bandwidth that scales with 1/FWHM.

    python3 focus/blurscore.py --dir frames/moonfocus_0924_preview --band 0.02,0.12 --low 0.005,0.02
Frequencies are cycles per plane pixel (plane px = 2 frame px).
"""
import argparse, glob, os, re, sys
import numpy as np
from scipy import ndimage

ap = argparse.ArgumentParser()
ap.add_argument("--dir", required=True); ap.add_argument("--glob", default="r*_p*.npy")
ap.add_argument("--band", default="0.02,0.12", help="mid band, cycles/plane px")
ap.add_argument("--low", default="0.004,0.02", help="scene-contrast band")
ap.add_argument("--noise", default="0.35,0.5", help="noise-floor band")
a = ap.parse_args()
b0, b1 = [float(v) for v in a.band.split(",")]; l0, l1 = [float(v) for v in a.low.split(",")]; n0, n1 = [float(v) for v in a.noise.split(",")]

def spectrum(g):
    g = ndimage.median_filter(g, 3)
    g = g - g.mean()
    win = np.outer(np.hanning(g.shape[0]), np.hanning(g.shape[1]))
    P = np.abs(np.fft.fft2(g * win)) ** 2 / (win ** 2).sum()
    fy = np.fft.fftfreq(g.shape[0])[:, None]; fx = np.fft.fftfreq(g.shape[1])[None, :]
    f = np.hypot(fx, fy)
    bins = np.linspace(0, 0.5, 101); idx = np.digitize(f.ravel(), bins) - 1
    prof = np.bincount(idx, P.ravel(), minlength=len(bins)) / np.maximum(np.bincount(idx, minlength=len(bins)), 1)
    return 0.5 * (bins[:-1] + bins[1:]), prof[:-1]

rows = []
for fn in sorted(glob.glob(os.path.join(a.dir, a.glob))):
    m = re.search(r"r(\d+)_p(\d+)", os.path.basename(fn))
    if not m: continue
    img = np.load(fn).astype(np.float64)
    g = img[0::2, 1::2]
    f, p = spectrum(g)
    noise = p[(f >= n0) & (f < n1)].mean()
    pc = np.clip(p - noise, 0, None)
    mid = pc[(f >= b0) & (f < b1)].mean(); low = pc[(f >= l0) & (f < l1)].mean()
    # bandwidth: first frequency where the corrected profile drops below 10% of the low-band mean
    above = np.nonzero((f >= l1) & (pc < 0.1 * low))[0]
    f10 = float(f[above[0]]) if above.size else float("nan")
    rows.append(dict(r=int(m.group(1)), pos=int(m.group(2)), mean=float(g.mean()), mid=mid, low=low, noise=noise, ratio=mid / max(low, 1e-9), f10=f10))
    print("r%d pos %6d: mean %6.0f  low %.3g  mid %.3g  noise %.3g  mid/low %.4f  f10 %.4f c/px" % (rows[-1]["r"], rows[-1]["pos"], rows[-1]["mean"], low, mid, noise, rows[-1]["ratio"], f10))
print("=== by position (median over rounds) ===")
byp = {}
for r in rows: byp.setdefault(r["pos"], []).append(r)
best = []
for pos in sorted(byp):
    rs = byp[pos]
    ratio = float(np.median([r["ratio"] for r in rs])); f10 = float(np.nanmedian([r["f10"] for r in rs])); mid = float(np.median([r["mid"] / r["mean"] ** 2 for r in rs]))
    best.append((pos, ratio, f10, mid))
    print("  %6d n=%d  mid/low %.4f  f10 %.4f  mid/mean^2 %.3g" % (pos, len(rs), ratio, f10, mid))
if best:
    print("best by mid/low: %d ; by f10: %d ; by mid/mean^2: %d" % (max(best, key=lambda t: t[1])[0], max(best, key=lambda t: (t[2] if t[2] == t[2] else -1))[0], max(best, key=lambda t: t[3])[0]))
