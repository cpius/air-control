#!/usr/bin/env python3
"""Whole-Moon mosaic: find the disc centre from the limb, shoot a grid of tiles by
register gotos, and assemble a quick-look.

    ASIAIR_HOST=192.168.1.36 python3 -u moon/moonmosaic.py --rows 5 --cols 8 --step-ns 8 --step-ew 5 --frames 3 --exp-ms 7 --gain 100
    python3 -u moon/moonmosaic.py --assemble ../telemetry/2026-09-27/moonmosaic/221249     # luminance quick-look
    python3 -u moon/moonmosaic.py --colour ../telemetry/2026-09-27/moonmosaic/221249       # full-res colour, north up

Geometry (2026-09-27, lens cell in the ADC, ADC reversed): preview bin 2 = 1920x1080 at
0.3732"/px = 11.94' x 6.72'; sky-east on the sensor (-0.070, -0.998) (x right, y down),
north = east turned +90 deg -- so the LONG axis runs north-south: rows step in Dec
(--step-ns, arcmin), columns in RA (--step-ew). Tiles whose footprint misses a circle of
semi-diameter + --margin are skipped. Order is column by column, serpentine, so N-S
neighbours are seconds apart. The Moon's own Dec motion (ephemeris) is added to each
target so the grid follows it; the rest of the drift is measured at assembly.

Centre: one frame with the limb in it; edge points of the half-level mask; a circle of
the ephemeris radius fitted with the radius FIXED (a 12' arc cannot fit its own radius).
Needs dark sky in the frame -- start with the limb in the field.

RA gotos are quantized to 1 s of time (~15"), Dec moves under-deliver ~12% on small
steps: both are far inside the overlaps (>=1.7'), and the assembly registers the tiles
on their overlaps anyway (phase correlation, least squares, feathered blend).
Tiles are saved raw (uint16 RGGB .npy) with a manifest.json; the quick-look is luminance
from 2x2 superpixels (0.746"/px).
"""
import argparse, datetime as dt, json, math, os, sys, time
import numpy as np
from scipy import ndimage, optimize
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import moonephem as me
from moonreg import bandpass, measure_shift

ap = argparse.ArgumentParser()
ap.add_argument("--rows", type=int, default=5, help="tiles north-south")
ap.add_argument("--cols", type=int, default=8, help="tiles east-west")
ap.add_argument("--step-ns", type=float, default=8.0, help="arcmin between rows (tile is 11.9' along N-S)")
ap.add_argument("--step-ew", type=float, default=5.0, help="arcmin between columns (tile is 6.7' along E-W)")
ap.add_argument("--margin", type=float, default=2.0, help="arcmin beyond the limb a tile must reach into to be shot")
ap.add_argument("--frames", type=int, default=3, help="frames per tile (after one discarded)")
ap.add_argument("--exp-ms", type=float, default=7.0); ap.add_argument("--gain", type=int, default=100)
ap.add_argument("--settle-s", type=float, default=1.0)
ap.add_argument("--scale", type=float, default=0.1866, help="bin-1 arcsec/px (driftscale)")
ap.add_argument("--east", default="-0.070,-0.998", help="sky east on the sensor (driftscale slide, 2026-09-27 21:50)")
ap.add_argument("--centre", default=None, help="RA_h,Dec_deg of the disc centre in REGISTER coords (skip the limb fit)")
ap.add_argument("--lat", type=float, default=55.689444); ap.add_argument("--lon", type=float, default=12.555278)
ap.add_argument("--assemble", default=None, help="assemble an existing capture directory (luminance quick-look) and exit")
ap.add_argument("--colour", default=None, help="build the full-resolution colour mosaic of a capture directory and exit")
ap.add_argument("--haze-offset", action="store_true", help="also solve a per-tile additive haze offset from the overlaps (2026-09-27: +5%% contrast in cloudy tiles but darkened the Crisium limb -- off by default)")
ap.add_argument("--sharpen", type=float, default=0.6, help="unsharp-mask amount for the display JPEG (0 = none; the TIFF is never sharpened)")
ap.add_argument("--outroot", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "telemetry", time.strftime("%Y-%m-%d"), "moonmosaic"))
a = ap.parse_args()
E = np.array([float(v) for v in a.east.split(",")]); E /= np.linalg.norm(E)
N = np.array([-E[1], E[0]])
BIN = 2; PX = a.scale * BIN                 # arcsec per bin-2 sensor px
BIAS = 3900.0


def log(s):
    print("%s  %s" % (time.strftime("%H:%M:%S"), s), flush=True)


def lum(img):
    """2x2 superpixel mean minus bias: luminance at half resolution (0.746"/px)."""
    f = img.astype(np.float32) - BIAS
    return 0.25 * (f[0::2, 0::2] + f[0::2, 1::2] + f[1::2, 0::2] + f[1::2, 1::2])


def moon_now(t=None):
    t = t or dt.datetime.now(dt.timezone.utc)
    return me.moon(t, a.lat, a.lon)


# ---------------------------------------------------------------- assembly
def sharpness(im):
    gy, gx = np.gradient(ndimage.gaussian_filter(im, 1.0))
    m = im > 0.3 * np.percentile(im, 99)
    return float(np.mean((gx * gx + gy * gy)[m]) / (np.mean(im[m]) ** 2 + 1e-9)) if m.any() else 0.0


def register(d, qmin=0.2, min_disc=0.10, max_resid=3.0, min_overlap=150):
    """Tiles (sharpest frame each) with positions in SUPERPIXELS and brightness gains.
    Robust: weighted least squares on overlap pairs, pairs off the solution by > max_resid px
    dropped and re-solved; tiles with no surviving pair get the correction (solved - nominal)
    interpolated in capture time from the others -- the error is mostly drift and the 1 s RA
    quantization, which change smoothly along the serpentine."""
    man = json.load(open(os.path.join(d, "manifest.json")))
    sp = 2 * PX
    tiles = []
    for t in man["tiles"]:
        if not t.get("files"):
            continue
        ims = [lum(np.load(os.path.join(d, f))) for f in t["files"]]
        s_ = [sharpness(im) for im in ims]; k = int(np.argmax(s_))
        tt = dict(t, img=ims[k], best=t["files"][k], sharp=s_[k])
        v = (t["off_e"] * E + t["off_n"] * N) * 60.0 / sp
        tt["nom"] = np.array([v[0], v[1]], dtype=float)
        tiles.append(tt)
    n = len(tiles); H, W = tiles[0]["img"].shape
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            ti, tj = tiles[i], tiles[j]
            di = np.round(tj["nom"] - ti["nom"]).astype(int)
            w, h = W - abs(di[0]), H - abs(di[1])
            if w < min_overlap or h < min_overlap:
                continue                                  # thin diagonal strips (94 px) gave +50..80 px nonsense, 2026-09-27
            x0i, y0i = max(0, di[0]), max(0, di[1]); x0j, y0j = max(0, -di[0]), max(0, -di[1])
            A = ti["img"][y0i:y0i + h, x0i:x0i + w]; B = tj["img"][y0j:y0j + h, x0j:x0j + w]
            if A.shape != B.shape:
                continue
            both = (A > 0.3 * np.percentile(ti["img"], 99)) & (B > 0.3 * np.percentile(tj["img"], 99))
            if both.mean() < min_disc:
                continue
            ddx, ddy, q = measure_shift(A, B, lo=1.5, hi=20.0)
            if q < qmin or abs(ddx) > 0.3 * w or abs(ddy) > 0.3 * h:
                continue
            a15, a85 = np.percentile(A[both], [15, 85]); b15, b85 = np.percentile(B[both], [15, 85])
            alpha = float((b85 - b15) / max(a85 - a15, 1.0)); beta = float(b85 - alpha * a85)
            lr = -math.log(max(alpha, 1e-3)) if a.haze_offset else \
                math.log(float(np.median(A[both])) / max(float(np.median(B[both])), 1.0))   # gain-only: match the median level
            wgt = q * math.sqrt(both.mean() * w * h / (W * H))      # quality x usable overlap
            pairs.append([i, j, di[0] - ddx, di[1] - ddy, wgt, lr, alpha, beta])
    ref = int(np.argmin([math.hypot(t["off_e"], t["off_n"]) for t in tiles]))
    for it in range(8):
        M = np.zeros((len(pairs) + 1, n)); bx = np.zeros(len(pairs) + 1); by = np.zeros(len(pairs) + 1)
        for r, (i, j, dx, dy, q, lr, al, be) in enumerate(pairs):
            M[r, j] = q; M[r, i] = -q; bx[r] = q * dx; by[r] = q * dy
        M[-1, ref] = 1; bx[-1] = tiles[ref]["nom"][0]; by[-1] = tiles[ref]["nom"][1]
        px_ = np.linalg.lstsq(M, bx, rcond=None)[0]; py_ = np.linalg.lstsq(M, by, rcond=None)[0]
        res = np.array([math.hypot(px_[j] - px_[i] - dx, py_[j] - py_[i] - dy) for (i, j, dx, dy, q, lr, al, be) in pairs])
        thr = max(max_resid, 3.0 * 1.4826 * float(np.median(res)))
        worst = np.argsort(res)[::-1][:max(1, len(pairs) // 10)]           # at most the worst 10% per pass
        bad = [int(k) for k in worst if res[k] > thr]
        log("  registration pass %d: %d pairs, residual median %.2f max %.1f px; threshold %.1f, dropping %d" % (it + 1, len(pairs), np.median(res), res.max(), thr, len(bad)))
        if not bad:
            break
        pairs = [p_ for k, p_ in enumerate(pairs) if k not in bad]
    # connectivity from the reference over the surviving pairs
    adj = {k: set() for k in range(n)}
    for (i, j, *_r) in pairs:
        adj[i].add(j); adj[j].add(i)
    conn, stack = {ref}, [ref]
    while stack:
        k = stack.pop()
        for q_ in adj[k] - conn:
            conn.add(q_); stack.append(q_)
    ts = np.array([t["t"] for t in tiles])
    cidx = sorted(conn, key=lambda k: ts[k])
    cx_ = np.array([px_[k] - tiles[k]["nom"][0] for k in cidx]); cy_ = np.array([py_[k] - tiles[k]["nom"][1] for k in cidx])
    for k, t in enumerate(tiles):
        if k in conn:
            t["pos"] = np.array([px_[k], py_[k]]); t["how"] = "solved"
        else:
            corr = np.array([np.interp(ts[k], ts[cidx], cx_), np.interp(ts[k], ts[cidx], cy_)])
            t["pos"] = t["nom"] + corr; t["how"] = "time-interpolated"
    loose = [(t["row"], t["col"]) for t in tiles if t["how"] != "solved"]
    log("  %d tiles solved, %d placed by the time-interpolated correction %s" % (len(conn), len(loose), loose))
    # photometry: tile' = g * (tile - o). Cloud both dims a tile (gain) and veils it with scattered
    # moonlight (offset); gain-only matching left the hazy tiles flat and soft (2026-09-27, upper left).
    # From each pair's B = alpha*A + beta:  log g_j - log g_i = -log alpha  and  o_j - alpha*o_i = beta.
    G = np.zeros((len(pairs) + 1, n)); bg = np.zeros(len(pairs) + 1)
    for r, (i, j, dx, dy, q, lr, al, be) in enumerate(pairs):
        G[r, j] = q; G[r, i] = -q; bg[r] = q * lr
    G[-1, ref] = 1
    lg = np.linalg.lstsq(G, bg, rcond=None)[0]
    O = np.zeros((len(pairs) + 1, n)); bo = np.zeros(len(pairs) + 1)
    for r, (i, j, dx, dy, q, lr, al, be) in enumerate(pairs):
        O[r, j] = q; O[r, i] = -q * al; bo[r] = q * be
    O[-1, ref] = 1
    off = np.linalg.lstsq(O, bo, rcond=None)[0]
    # anchor: the clearest tiles carry no extra veil -> shift so the lowest offsets (in output units) are ~0
    out_off = np.array([math.exp(lg[k]) * off[k] for k in range(n)])
    base = float(np.percentile([out_off[k] for k in conn], 10))
    for k, t in enumerate(tiles):
        if k in conn:
            t["gain"] = math.exp(lg[k]); t["offset"] = float(off[k] - base / t["gain"]) if a.haze_offset else 0.0
        else:
            t["gain"] = float(np.exp(np.median(lg[list(conn)]))); t["offset"] = 0.0
    oo = [t["gain"] * t["offset"] for t in tiles]
    log("  photometry: gains %.2f..%.2f, haze offsets %.0f..%.0f ADU (output units)" % (min(t["gain"] for t in tiles), max(t["gain"] for t in tiles), min(oo), max(oo)))
    json.dump([dict(row=t["row"], col=t["col"], best=t["best"], pos=[float(v) for v in t["pos"]], nom=[float(v) for v in t["nom"]],
                    gain=t["gain"], offset=t["offset"], how=t["how"]) for t in tiles], open(os.path.join(d, "registration.json"), "w"), indent=1)
    return tiles, (H, W)


def feather(H, W, ramp):
    wy = np.minimum(np.arange(H), np.arange(H)[::-1]) + 1.0; wx = np.minimum(np.arange(W), np.arange(W)[::-1]) + 1.0
    return np.clip(np.minimum.outer(wy, wx) / ramp, 0, 1).astype(np.float32)


def assemble(d):
    tiles, (H, W) = register(d)
    sp = 2 * PX
    xs = [t["pos"][0] for t in tiles]; ys = [t["pos"][1] for t in tiles]
    x0 = min(xs) - W / 2 - 4; y0 = min(ys) - H / 2 - 4
    CW = int(max(xs) - min(xs) + W + 8); CH = int(max(ys) - min(ys) + H + 8)
    acc = np.zeros((CH, CW), np.float64); wacc = np.zeros((CH, CW), np.float64); wt = feather(H, W, 60.0)
    for t in tiles:
        cx, cy = t["pos"][0] - W / 2 - x0, t["pos"][1] - H / 2 - y0
        ix, iy = int(math.floor(cx)), int(math.floor(cy))
        im = ndimage.shift((t["img"] - t["offset"]) * t["gain"], (cy - iy, cx - ix), order=1, mode="nearest")
        acc[iy:iy + H, ix:ix + W] += im * wt; wacc[iy:iy + H, ix:ix + W] += wt
    mos = np.where(wacc > 0, acc / np.maximum(wacc, 1e-9), 0).astype(np.float32)
    np.save(os.path.join(d, "mosaic_lum.npy"), mos)
    import cv2
    lo, hi = np.percentile(mos[wacc > 0], [0.5, 99.95])
    u8 = (np.clip((mos - lo) / (hi - lo), 0, 1) ** (1 / 1.6) * 255).astype(np.uint8)
    cv2.imwrite(os.path.join(d, "mosaic_quicklook.png"), u8)
    cv2.imwrite(os.path.join(d, "mosaic_quicklook_small.png"), cv2.resize(u8, (CW // 3, CH // 3), interpolation=cv2.INTER_AREA))
    log("mosaic %dx%d superpixels (%.1f' x %.1f') -> %s" % (CW, CH, CW * sp / 60, CH * sp / 60, os.path.join(d, "mosaic_quicklook.png")))
    return os.path.join(d, "mosaic_quicklook.png")


def colour(d, sharpen=0.6, gamma=1.25):
    """Full-resolution colour mosaic from the raw RGGB tiles (bin-2 frame px, 0.373"/px):
    edge-aware debayer (OpenCV BayerBG2RGB_EA = this sensor's RGGB), positions x2 from
    register(), per-tile cloud gain, feathered blend, R and B aligned to G on the disc
    (residual ADC / atmospheric dispersion), white balance neutral on the disc, sky beyond
    the limb set to black, rotated north up / east left. Writes a linear 16-bit TIFF and a
    display JPEG (gamma, light unsharp mask) plus a small preview."""
    import cv2
    tiles, (h, w) = register(d)
    H, W = 2 * h, 2 * w
    xs = [2 * t["pos"][0] for t in tiles]; ys = [2 * t["pos"][1] for t in tiles]
    x0 = min(xs) - W / 2 - 8; y0 = min(ys) - H / 2 - 8
    CW = int(max(xs) - min(xs) + W + 16); CH = int(max(ys) - min(ys) + H + 16)
    log("colour canvas %d x %d px (%.1f' x %.1f') from %d tiles" % (CW, CH, CW * PX / 60, CH * PX / 60, len(tiles)))
    acc = np.zeros((CH, CW, 3), np.float32); wacc = np.zeros((CH, CW), np.float32); wt = feather(H, W, 120.0)
    for k, t in enumerate(tiles):
        raw = np.clip(np.load(os.path.join(d, t["best"])).astype(np.float32) - BIAS, 0, 65535).astype(np.uint16)
        rgb = cv2.cvtColor(raw, cv2.COLOR_BayerBG2RGB_EA).astype(np.float32)
        lvl = np.array([np.median(rgb[..., ch]) for ch in range(3)]); lvl_l = max(float(lvl.mean()), 1.0)
        rgb = (rgb - (t["offset"] * lvl / lvl_l)[None, None, :]) * t["gain"]   # haze has the Moon's colour
        cx, cy = 2 * t["pos"][0] - W / 2 - x0, 2 * t["pos"][1] - H / 2 - y0
        ix, iy = int(math.floor(cx)), int(math.floor(cy))
        M = np.float32([[1, 0, cx - ix], [0, 1, cy - iy]])
        rgb = cv2.warpAffine(rgb, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        acc[iy:iy + H, ix:ix + W] += rgb * wt[..., None]; wacc[iy:iy + H, ix:ix + W] += wt
        if k % 8 == 0:
            log("  placed %d/%d tiles" % (k + 1, len(tiles)))
    mos = acc / np.maximum(wacc, 1e-6)[..., None]; del acc
    # disc mask: a CIRCLE fitted to the limb, not a brightness threshold -- dark maria at the limb
    # (Crisium, Smythii) fall under any threshold and got blacked out on the first build
    L = mos.mean(axis=2)
    Ls = ndimage.gaussian_filter(L, 3)
    top = float(np.percentile(L[wacc > 0], 99))
    thr = ndimage.binary_fill_holes(ndimage.binary_opening(Ls > 0.35 * top, iterations=3))
    lab, nl = ndimage.label(thr)
    if nl > 1:
        thr = lab == (1 + int(np.argmax(ndimage.sum(thr, lab, range(1, nl + 1)))))
    edge = thr ^ ndimage.binary_erosion(thr, iterations=1)
    ey, ex = np.nonzero(edge)
    sel = np.random.default_rng(0).choice(ex.size, min(ex.size, 20000), replace=False); ex, ey = ex[sel].astype(float), ey[sel].astype(float)
    R0 = man_R = None
    cy_, cx_ = ndimage.center_of_mass(thr); R0 = math.sqrt(thr.sum() / math.pi) * 1.02
    c = np.array([cx_, cy_, R0])
    keep = np.ones(ex.size, bool)
    for it in range(5):
        c = optimize.least_squares(lambda p: np.hypot(ex[keep] - p[0], ey[keep] - p[1]) - p[2], c, loss="soft_l1", f_scale=3.0).x
        r = np.hypot(ex - c[0], ey - c[1]) - c[2]
        keep = np.abs(r) < 6.0                       # the limb; bites into maria lie well inside the circle
    # refine on the TRUE limb: rays every 0.25 deg, each scanned inward from the sky to the first
    # crossing of half-way between the LOCAL sky and the LOCAL surface just inside (dark maria at
    # the limb and uneven cloud glow both handled); robust circle fit to those points
    pts = []
    rr = np.arange(c[2] - 220, c[2] + 220, 0.5)
    for th in np.linspace(0, 2 * np.pi, 1440, endpoint=False):
        xs_r = c[0] + rr * math.cos(th); ys_r = c[1] + rr * math.sin(th)
        if xs_r.min() < 1 or ys_r.min() < 1 or xs_r.max() > CW - 2 or ys_r.max() > CH - 2:
            continue
        prof = ndimage.map_coordinates(Ls, [ys_r, xs_r], order=1)
        sky = float(np.median(prof[-120:]))                           # outermost 60 px
        above = np.nonzero(prof[::-1] > sky + 0.25 * (top - sky))[0]   # first clearly-lunar sample from outside
        if not above.size:
            continue
        k0 = len(prof) - 1 - above[0]
        surf = float(np.median(prof[max(k0 - 120, 0):max(k0 - 30, 1)]))  # 15-60 px inside it
        if surf - sky < 0.15 * top:
            continue
        mid = sky + 0.5 * (surf - sky)
        a_ = np.nonzero(prof[::-1] > mid)[0]
        if not a_.size:
            continue
        kc = len(prof) - 1 - a_[0]
        if kc + 1 >= len(prof):
            continue
        f = kc + (prof[kc] - mid) / max(prof[kc] - prof[kc + 1], 1e-6)
        rc = rr[0] + 0.5 * f
        pts.append((c[0] + rc * math.cos(th), c[1] + rc * math.sin(th), th))
    pts = np.array(pts); keep = np.ones(len(pts), bool)
    for it in range(4):
        c = optimize.least_squares(lambda p: np.hypot(pts[keep, 0] - p[0], pts[keep, 1] - p[1]) - p[2], c, loss="soft_l1", f_scale=2.0).x
        r = np.hypot(pts[:, 0] - c[0], pts[:, 1] - c[1]) - c[2]
        keep = np.abs(r) < max(4.0, 3 * 1.4826 * float(np.median(np.abs(r[keep]))))
    log("  limb circle (rays): centre (%.0f, %.0f), radius %.1f px = %.1f\" -- %d of %d rays on it, rms %.2f px" %
        (c[0], c[1], c[2], c[2] * PX, keep.sum(), len(pts), float(np.std(r[keep]))))
    # the mask follows the measured limb angle by angle (median over ~2 deg), not the circle:
    # the mosaic's limb departs from a circle by ~20 px rms (refraction, residual registration)
    th_ = pts[:, 2]; rl = np.hypot(pts[:, 0] - c[0], pts[:, 1] - c[1])
    o = np.argsort(th_); th_, rl = th_[o], rl[o]
    # OUTER envelope, smooth: on the terminator-side limb (waning, east) the rays stop on lit crater
    # rims inside night shadow, and a per-ray mask cut square notches into the Moon (2026-09-27).
    # Rolling 80th percentile over +-7.5 deg, Gaussian over ~3 deg, kept within [circle-10, circle+25].
    n_ = rl.size; win = max(3, int(round(15.0 / 360.0 * n_))) | 1
    ext = np.concatenate([rl[-win:], rl, rl[:win]])
    env = np.array([np.percentile(ext[k:k + 2 * win + 1], 80) for k in range(n_)])
    env = ndimage.gaussian_filter1d(env, sigma=max(1.0, 3.0 / 360.0 * n_), mode="wrap")
    rl_s = np.clip(env, c[2] - 10.0, c[2] + 25.0)
    Y, X = np.ogrid[:CH, :CW]
    ang = np.mod(np.arctan2(Y - c[1], X - c[0]), 2 * np.pi)
    rlim = np.interp(ang, np.concatenate([th_ - 2 * np.pi, th_, th_ + 2 * np.pi]), np.concatenate([rl_s, rl_s, rl_s]))
    dist = np.hypot(X - c[0], Y - c[1]) - rlim
    disc = dist < 0
    log("  limb radius varies %.0f..%.0f px around the disc (circle %.0f)" % (rl_s.min(), rl_s.max(), c[2]))
    ys_, xs_ = np.nonzero(disc); cy0, cx0 = int(np.median(ys_)), int(np.median(xs_))
    half = 1024
    win = (slice(max(cy0 - half, 0), cy0 + half), slice(max(cx0 - half, 0), cx0 + half))
    Gc = mos[win][..., 1]
    for ch, nm in ((0, "R"), (2, "B")):
        dx, dy, q = measure_shift(Gc, mos[win][..., ch], lo=1.5, hi=20.0)
        log("  %s relative to G: (%+.2f, %+.2f) px = %.2f\" (q %.2f) -- aligned" % (nm, dx, dy, math.hypot(dx, dy) * PX, q))
        mos[..., ch] = ndimage.shift(mos[..., ch], (-dy, -dx), order=1, mode="nearest")
    # white balance: neutral median on the disc
    med = np.array([np.median(mos[..., c][disc]) for c in range(3)])
    wb = med[1] / med
    log("  white balance (disc median neutral): R x%.3f  G x1  B x%.3f" % (wb[0], wb[2]))
    mos *= wb[None, None, :]
    # sky beyond the limb -> black, with a soft edge 6 px outside the limb
    soft = np.clip(1.0 - (dist - 5.0) / 5.0, 0, 1).astype(np.float32)   # keep 5 px beyond the measured limb (seeing), fade over 5
    mos *= soft[..., None]
    # north up, east left: rotate so the sensor's north vector points up
    ang = math.degrees(math.atan2(-N[1], N[0]))                    # N's angle above +x in display (y up) terms
    rot = 90.0 - ang
    c = (CW / 2.0, CH / 2.0)
    Mr = cv2.getRotationMatrix2D(c, rot, 1.0)
    cos_, sin_ = abs(Mr[0, 0]), abs(Mr[0, 1]); nW, nH = int(CH * sin_ + CW * cos_), int(CH * cos_ + CW * sin_)
    Mr[0, 2] += nW / 2 - c[0]; Mr[1, 2] += nH / 2 - c[1]
    mos = cv2.warpAffine(mos, Mr, (nW, nH), flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0))
    dmask = cv2.warpAffine((dist < 5.0).astype(np.uint8), Mr, (nW, nH), flags=cv2.INTER_NEAREST) > 0
    ys_, xs_ = np.nonzero(dmask); pad = 60
    crop = (slice(max(ys_.min() - pad, 0), ys_.max() + pad), slice(max(xs_.min() - pad, 0), xs_.max() + pad))
    mos = mos[crop]
    log("  rotated %.1f deg (north up, east left); cropped to %d x %d px, disc %d px across" % (rot, mos.shape[1], mos.shape[0], xs_.max() - xs_.min()))
    # outputs: linear 16-bit TIFF, display JPEG
    top = float(np.percentile(mos[mos.sum(axis=2) > 0], 99.9))
    lin = np.clip(mos / top, 0, 1)
    cv2.imwrite(os.path.join(d, "moon_colour_linear16.tif"), cv2.cvtColor((lin * 65535).astype(np.uint16), cv2.COLOR_RGB2BGR))
    disp = lin ** (1 / gamma)
    if sharpen > 0:
        blur = cv2.GaussianBlur(disp, (0, 0), 1.6)
        disp = np.clip(disp + sharpen * (disp - blur), 0, 1)
    u8 = cv2.cvtColor((disp * 255 + 0.5).astype(np.uint8), cv2.COLOR_RGB2BGR)
    cv2.imwrite(os.path.join(d, "moon_colour.jpg"), u8, [cv2.IMWRITE_JPEG_QUALITY, 95])
    cv2.imwrite(os.path.join(d, "moon_colour_small.jpg"), cv2.resize(u8, (u8.shape[1] // 4, u8.shape[0] // 4), interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 92])
    log("wrote moon_colour_linear16.tif, moon_colour.jpg (%d x %d), moon_colour_small.jpg in %s" % (u8.shape[1], u8.shape[0], d))


if a.assemble:
    assemble(a.assemble); sys.exit(0)
if a.colour:
    colour(a.colour, sharpen=a.sharpen); sys.exit(0)

# ---------------------------------------------------------------- capture
from daypipes import host, Pipes, save_png
from mount import Mount
try:
    from recorder import rec
except Exception:
    rec = None


def fit_centre(img, R_px):
    """Disc centre (bin-2 px, frame coords) from the limb in one frame, radius fixed."""
    g = ndimage.gaussian_filter(lum(img), 2.0)                   # superpixels
    top = float(np.percentile(g, 97))
    mask = g > 0.5 * top
    if not (0.05 < mask.mean() < 0.95) or float(np.percentile(g, 0.5)) > 0.3 * top:
        return None
    edge = mask ^ ndimage.binary_erosion(mask, iterations=1)
    edge[:3, :] = edge[-3:, :] = False; edge[:, :3] = edge[:, -3:] = False
    ys, xs = np.nonzero(edge)
    if xs.size < 100:
        return None
    Rs = R_px / 2.0                                               # superpixels
    H, W = g.shape
    cyy, cxx = ndimage.center_of_mass(mask)
    nvec = np.array([cxx - W / 2, cyy - H / 2]); nvec /= (np.linalg.norm(nvec) + 1e-9)
    # start on the disc side, radius away from the edge point nearest the frame centre along that direction
    c0 = np.array([W / 2, H / 2]) + nvec * Rs
    res = optimize.least_squares(lambda c: np.hypot(xs - c[0], ys - c[1]) - Rs, c0, loss="soft_l1", f_scale=2.0)
    c = res.x
    r = np.hypot(xs - c[0], ys - c[1]) - Rs
    return dict(cx=2 * c[0], cy=2 * c[1], rms=float(np.std(r)) * 2, n=int(xs.size))


def mount_do(fn, tries=3):
    """Run fn(m) on the mount, reconnecting if 4400 dropped the socket (it closes after ~15 s idle)."""
    global m
    for k in range(tries):
        try:
            return fn(m)
        except (BrokenPipeError, ConnectionError, OSError, TimeoutError) as e:
            log("  mount socket lost (%s) -- reconnecting (%d/%d)" % (e.__class__.__name__, k + 1, tries))
            try: m.close()
            except Exception: pass
            time.sleep(1.0); m = Mount(host())
    return fn(m)


os.makedirs(a.outroot, exist_ok=True)
D = os.path.join(a.outroot, time.strftime("%H%M%S")); os.makedirs(D)
p = Pipes(); m = Mount(host())
man = dict(started=time.strftime("%Y-%m-%d %H:%M:%S"), args=vars(a), east=list(E), north=list(N), px_arcsec=PX, tiles=[])
t0 = time.time()
try:
    s = m.state()
    if not s["is_enable_track"] or m._r("scope_get_track_mode").get("index") != 2:
        m._r("scope_set_track_state", [True]); m._r("scope_set_track_mode", ["Lunar"]); log("tracking set ON, Lunar")
    fi = p.c("get_focuser_info"); man["eaf"] = fi.get("position"); man["eaf_temp"] = fi.get("temperature")
    mo = moon_now(); R_as = mo["diam_arcmin"] * 30.0
    log("Moon alt %.1f, semi-diameter %.1f\"; EAF %s; register RA %.4f Dec %+.4f" % (mo["alt"], R_as, man["eaf"], s["RA"], s["Dec"]))
    p.setup("preview", a.exp_ms / 1000.0, a.gain, BIN); p.grab(timeout=40)
    if a.centre:
        ra_c, dec_c = [float(v) for v in a.centre.split(",")]
        log("centre given: register RA %.4f Dec %+.4f" % (ra_c, dec_c))
    else:
        img = p.grab(timeout=40)[0]
        fc = fit_centre(img, R_as / PX)
        if fc is None:
            sys.exit("no limb with dark sky in the frame -- point at the limb first, or pass --centre")
        d = np.array([fc["cx"] - 960, fc["cy"] - 540]) * PX / 60.0      # arcmin on the sensor, from the frame centre
        dE, dN = float(d @ E), float(d @ N)
        ra_c = s["RA"] + dE / 60.0 / 15.0 / math.cos(math.radians(s["Dec"])); dec_c = s["Dec"] + dN / 60.0
        log("limb fit: %d edge points, rms %.1f px; disc centre %+.1f' E %+.1f' N of the frame -> register RA %.4f Dec %+.4f" % (fc["n"], fc["rms"], dE, dN, ra_c, dec_c))
    man["centre_register"] = [ra_c, dec_c]
    # grid (arcmin, centre-relative); skip tiles that miss the disc + margin
    hw, hh = 1920 * PX / 120.0, 1080 * PX / 120.0          # half-sizes along x (N-S) and y (E-W), arcmin
    Rm = R_as / 60.0 + a.margin
    grid = []
    for ci in range(a.cols):
        oe = (ci - (a.cols - 1) / 2.0) * a.step_ew
        rows = list(range(a.rows)) if ci % 2 == 0 else list(range(a.rows))[::-1]
        for ri in rows:
            on = (ri - (a.rows - 1) / 2.0) * a.step_ns
            nx = max(abs(on) - hw, 0.0); ny = max(abs(oe) - hh, 0.0)
            if math.hypot(nx, ny) > Rm:
                continue
            grid.append((ri, ci, oe, on))
    log("grid %d x %d at %.1f'/%.1f' -> %d tiles to shoot (%d frames each)" % (a.rows, a.cols, a.step_ns, a.step_ew, len(grid), a.frames))
    if rec: rec.note("moon mosaic: %d tiles" % len(grid), device="main camera")
    dec_rate = (moon_now(dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1))["dec"] - moon_now()["dec"])  # deg/min
    tg = time.time()
    for k, (ri, ci, oe, on) in enumerate(grid):
        tmin = (time.time() - tg) / 60.0
        ra = ra_c + oe / 60.0 / 15.0 / math.cos(math.radians(dec_c)); dec = dec_c + on / 60.0 + dec_rate * tmin
        mount_do(lambda mm: mm.goto(ra, dec, timeout=60)); time.sleep(a.settle_s)
        st = mount_do(lambda mm: mm.state())
        if not st["is_enable_track"]:
            mount_do(lambda mm: (mm._r("scope_set_track_state", [True]), mm._r("scope_set_track_mode", ["Lunar"]))); log("  tracking was OFF -- re-enabled")
        files = []; lv = []                                         # each preview grab starts a NEW exposure, after the settle
        for f in range(a.frames):
            img, w, h, info = p.grab(timeout=40)
            mount_do(lambda mm: mm.state())                         # keepalive: a tile's frames outlast 4400's idle timeout
            fn = "tile_r%d_c%d_f%d.npy" % (ri, ci, f)
            np.save(os.path.join(D, fn), np.asarray(img, dtype=np.uint16)); files.append(fn)
            L = lum(img); lv.append((float(np.percentile(L, 99)), float((L > 3000).mean())))
        disc_frac = lv[-1][1]
        if rec:
            try: rec.artifact("preview", save_png(img, os.path.join(D, "tile_r%d_c%d.png" % (ri, ci)), shrink=2), device="main camera", meta={"tool": "moonmosaic", "tile": [ri, ci]})
            except Exception: pass
        man["tiles"].append(dict(row=ri, col=ci, off_e=oe, off_n=on, ra=ra, dec=dec, reg_ra=st["RA"], reg_dec=st["Dec"], t=time.time(), files=files,
                                 p99=[v[0] for v in lv], disc_frac=disc_frac))
        json.dump(man, open(os.path.join(D, "manifest.json"), "w"), indent=1)
        log("  tile %2d/%d r%d c%d  (%+5.1f' E %+5.1f' N)  p99 %s  disc %3.0f%%  [%.0f s]" %
            (k + 1, len(grid), ri, ci, oe, on, "/".join("%.0f" % v[0] for v in lv), 100 * disc_frac, time.time() - t0))
    log("captured %d tiles in %.0f s -> %s" % (len(grid), time.time() - tg, D))
    man["finished"] = time.strftime("%Y-%m-%d %H:%M:%S"); json.dump(man, open(os.path.join(D, "manifest.json"), "w"), indent=1)
    # back to the centre
    mount_do(lambda mm: mm.goto(ra_c, dec_c + dec_rate * (time.time() - tg) / 60.0, timeout=60))
finally:
    try: p.close()
    except Exception: pass
    m.close()
log("assembling the quick-look ...")
assemble(D)
