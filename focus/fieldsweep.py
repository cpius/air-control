#!/usr/bin/env python3
"""Interleaved focus sweep on a rich star field: median FWHM of many SNR-gated stars per frame.

Preview page, bin 2, 16-bit RGGB. A 2x2 box sum of the raw mosaic is luminance at full bin-2
sampling (every 2x2 window holds R+G+G+B, so the CFA checker vanishes). Stars: local maxima,
Gaussian-weighted adaptive moments (the same method as ~/ASICAP/tools/framecheck.py, copied here so
the repo does not depend on it), SNR >= 20, saturated stars dropped. Every EAF move arrives from
above (daypipes.move_to, backlash). Rounds visit the positions in alternating order, and both
FWHM and concentration (peak/flux) are printed -- they must agree.

Hot pixels pass the gate as 1.7 px "stars" in defocused frames. Build a map once from a few
grossly defocused frames of the same night (a focused star would be flagged too) and pass it in:

    python3 focus/fieldsweep.py --make-hotmap hot.npy --from-npy frames/*_eaf44270_*.npy ...
    ASIAIR_HOST=... EAF_MIN=0 EAF_MAX=100000 python3 -u focus/fieldsweep.py \\
        --pos 22500,22600,22700,22800,22900,23000,23100 --rounds 3 --hotmap hot.npy

2026-10-19, f/6.3 reducer: 22000-24000 x400 x2 rounds, then x100 x3 rounds, then a 4-point verify
-- all put the vertex at 22800-22809, FWHM 4.1 bin-2 px = 3.7" over 110-150 stars. +-100 steps
already costs 10-40%, and the box (BOX=9) caps widths near 10-12 px, so the V flattens beyond
~+-300 steps: use discscan.py for anything coarser.
"""
import argparse, math, os, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

ap = argparse.ArgumentParser()
ap.add_argument("--pos", help="comma list of EAF positions")
ap.add_argument("--rounds", type=int, default=2)
ap.add_argument("--exp", type=float, default=2.0); ap.add_argument("--gain", type=int, default=100)
ap.add_argument("--scale", type=float, default=0.887, help="arcsec per bin-2 px (reducer: 0.887 by ASTAP 2026-10-19)")
ap.add_argument("--snr", type=float, default=20.0)
ap.add_argument("--hotmap", default=None, help=".npy bool map (raw bin-2 frame shape) of fixed hot pixels to replace")
ap.add_argument("--park", default="best", help="'best', 'none', or an EAF position")
ap.add_argument("--tag", default="fsweep")
ap.add_argument("--outdir", default=os.path.expanduser("~/ASICAP/telemetry/%s/frames" % time.strftime("%Y-%m-%d")))
ap.add_argument("--make-hotmap", default=None, metavar="OUT", help="build a hot-pixel map from --from-npy frames and exit")
ap.add_argument("--from-npy", nargs="+", default=[], help="grossly defocused raw frames for --make-hotmap")
a = ap.parse_args()
BOX = 9
SIG2FWHM = 2.0 * math.sqrt(2.0 * math.log(2.0))


def make_hotmap(out, files, k=8.0):
    """Raw px far above the median of their same-colour 3x3 neighbourhood, in any of the frames."""
    votes = None
    for f in files:
        img = np.load(f).astype(np.float32)
        hot = np.zeros(img.shape, bool)
        for dy in (0, 1):
            for dx in (0, 1):
                s = img[dy::2, dx::2]
                r = s - ndimage.median_filter(s, size=3)
                hot[dy::2, dx::2] = r > k * 1.4826 * np.median(np.abs(r))
        votes = hot.astype(int) if votes is None else votes + hot
        print("%s: %d hot" % (os.path.basename(f), hot.sum()))
    np.save(out, votes >= 1)
    print("%s: %d px" % (out, (votes >= 1).sum()))


if a.make_hotmap:
    if not a.from_npy:
        sys.exit("--make-hotmap needs --from-npy frames")
    make_hotmap(a.make_hotmap, a.from_npy)
    sys.exit(0)
if not a.pos:
    sys.exit("--pos is required")

from daypipes import Pipes, log, move_to, focuser_pos
os.makedirs(a.outdir, exist_ok=True)
POS = [int(v) for v in a.pos.split(",")]
HOT = np.load(a.hotmap) if a.hotmap else None


def background(g, box=64):
    """Coarse-grid median and MAD sigma, bilinearly upsampled (framecheck.py)."""
    h, w = g.shape
    ny, nx = h // box, w // box
    flat = g[:ny * box, :nx * box].reshape(ny, box, nx, box).swapaxes(1, 2).reshape(ny, nx, -1)
    med = np.median(flat, axis=2)
    mad = np.median(np.abs(flat - med[:, :, None]), axis=2) * 1.4826
    ys, xs = np.linspace(0, ny - 1, h), np.linspace(0, nx - 1, w)

    def up(t):
        r = np.empty((h, t.shape[1]), np.float32)
        for c in range(t.shape[1]):
            r[:, c] = np.interp(ys, np.arange(ny), t[:, c])
        o = np.empty((h, w), np.float32)
        for rr in range(h):
            o[rr] = np.interp(xs, np.arange(nx), r[rr])
        return o

    return up(med), up(mad)


def peaks(g, bg, sig, thresh=8.0, edge=BOX + 2):
    """Local maxima above thresh*sigma, 3x3 strict (framecheck.py)."""
    d = g - bg
    m = d > thresh * sig
    m[:edge] = m[-edge:] = False
    m[:, :edge] = m[:, -edge:] = False
    c = d[1:-1, 1:-1]
    hi = np.ones_like(c, bool)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy or dx:
                hi &= c >= d[1 + dy:d.shape[0] - 1 + dy, 1 + dx:d.shape[1] - 1 + dx]
    m[1:-1, 1:-1] &= hi
    ys, xs = np.nonzero(m)
    return ys, xs, d


def adaptive(cut, iters=12):
    """Gaussian-weighted moments iterated to convergence -- the Gaussian fit without a solver
    (framecheck.py). Returns (sigma_major, sigma_minor, theta, flux, peak) in px, or None."""
    n = cut.shape[0]
    c = (n - 1) / 2.0
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32)
    x0, y0, sxx, syy, sxy = c, c, 4.0, 4.0, 0.0
    for _ in range(iters):
        det = sxx * syy - sxy * sxy
        if det <= 1e-6:
            return None
        dx, dy = xx - x0, yy - y0
        r2 = (syy * dx * dx - 2 * sxy * dx * dy + sxx * dy * dy) / det
        f = cut * np.exp(-0.5 * np.clip(r2, 0, 40))
        s = f.sum()
        if s <= 0:
            return None
        nx0, ny0 = float((f * xx).sum() / s), float((f * yy).sum() / s)
        dx, dy = xx - nx0, yy - ny0
        mxx = float((f * dx * dx).sum() / s) * 2.0          # weighted moments are half the true ones
        myy = float((f * dy * dy).sum() / s) * 2.0
        mxy = float((f * dx * dy).sum() / s) * 2.0
        if mxx <= 0 or myy <= 0:
            return None
        moved = abs(nx0 - x0) + abs(ny0 - y0) + abs(mxx - sxx) + abs(myy - syy)
        x0, y0, sxx, syy, sxy = nx0, ny0, mxx, myy, mxy
        if moved < 1e-3:
            break
    if abs(x0 - c) > 3 or abs(y0 - c) > 3:
        return None
    tr, dif = sxx + syy, sxx - syy
    rad = math.sqrt(max(dif * dif + 4 * sxy * sxy, 0.0))
    v1, v2 = (tr + rad) / 2, (tr - rad) / 2
    if v2 <= 0.05:
        return None
    return math.sqrt(v1), math.sqrt(v2), 0.5 * math.atan2(2 * sxy, dif), float(cut.sum()), float(cut.max())


def lum(img):
    f = img.astype(np.float32)
    if HOT is not None and HOT.shape == f.shape:     # fixed hot pixels -> median of the same-colour ring
        for y, x in zip(*np.nonzero(HOT)):
            f[y, x] = np.median(f[max(y - 2, y % 2):y + 3:2, max(x - 2, x % 2):x + 3:2])
    return f[:-1, :-1] + f[1:, :-1] + f[:-1, 1:] + f[1:, 1:]


def measure(img):
    L = lum(img)
    satL = ndimage.binary_dilation((img >= 65000)[:-1, :-1], iterations=6)
    bgm, sigm = background(L)
    ys, xs, d = peaks(L, bgm, sigm)
    stars = []
    for i in np.argsort(-d[ys, xs])[:1500]:
        y, x = int(ys[i]), int(xs[i])
        if satL[y, x]:
            continue
        cut = d[y - BOX:y + BOX + 1, x - BOX:x + BOX + 1]
        if cut.shape != (2 * BOX + 1, 2 * BOX + 1):
            continue
        r = adaptive(np.clip(cut, 0, None))
        if r is None:
            continue
        smaj, smin, th, flux, peak = r
        noise = math.sqrt(max(math.pi * smaj * smin * 4, 1)) * float(sigm[y, x])
        if noise <= 0 or flux / noise < a.snr:
            continue
        stars.append((smaj * SIG2FWHM, smin * SIG2FWHM, peak / max(flux, 1)))
        if len(stars) >= 300:
            break
    if not stars:
        return dict(n=0, ndet=len(ys))
    s = np.array(stars)
    fw = np.sqrt(s[:, 0] * s[:, 1])
    return dict(n=len(s), fwhm=float(np.median(fw)), fwhm_as=float(np.median(fw)) * a.scale,
                ecc=float(np.median(s[:, 0] / s[:, 1])), conc=float(np.median(s[:, 2])) * 1000,
                p25=float(np.percentile(fw, 25)), p75=float(np.percentile(fw, 75)), ndet=len(ys))


res = {p: [] for p in POS}
p = Pipes()
t0 = time.time()
try:
    p.setup("preview", a.exp, a.gain, 2)
    for rnd in range(a.rounds):
        seq = POS if rnd % 2 == 0 else POS[::-1]
        log("round %d/%d: %s" % (rnd + 1, a.rounds, seq))
        for pos in seq:
            move_to(p, pos)
            img, w, h, info = p.grab()
            got = focuser_pos(p)
            m = measure(img)
            res[pos].append(m)
            fn = os.path.join(a.outdir, "%s_%s_r%d_eaf%d" % (time.strftime("%H%M%S"), a.tag, rnd, got))
            np.save(fn + ".npy", img.astype(np.uint16))
            if m["n"]:
                log("  EAF %6d  stars %3d/%4d  FWHM %5.2f px = %4.2f\"  (IQR %.2f-%.2f)  ecc %.2f  conc %.2f  fresh=%s  [%.0fs]" % (
                    got, m["n"], m["ndet"], m["fwhm"], m["fwhm_as"], m["p25"], m["p75"], m["ecc"], m["conc"], info.get("fresh"), time.time() - t0))
            else:
                log("  EAF %6d  NO STARS passed the gate  fresh=%s" % (got, info.get("fresh")))
    log("SUMMARY (median over rounds)")
    xs, ys = [], []
    for pos in POS:
        v = [m["fwhm"] for m in res[pos] if m["n"]]
        c = [m["conc"] for m in res[pos] if m["n"]]
        if v:
            xs.append(pos); ys.append(float(np.median(v)))
            log("  EAF %6d  FWHM %s  -> %.2f px (%.2f\")   conc %s" % (pos, " ".join("%.2f" % x for x in v), np.median(v), np.median(v) * a.scale, " ".join("%.2f" % x for x in c)))
    best = xs[int(np.argmin(ys))] if xs else None
    vert = None
    if len(xs) >= 3:
        i = int(np.argmin(ys)); lo, hi = max(0, i - 2), min(len(xs), i + 3)
        if hi - lo >= 3:
            cf = np.polyfit(xs[lo:hi], ys[lo:hi], 2)
            if cf[0] > 0:
                vert = -cf[1] / (2 * cf[0])
    log("best measured %s ; parabola vertex %s" % (best, "%.0f" % vert if vert else "n/a"))
    park = None if a.park == "none" else (best if a.park == "best" else int(a.park))
    if park is not None:
        move_to(p, park); log("parked EAF at %d (arrived from above)" % focuser_pos(p))
finally:
    p.close()
