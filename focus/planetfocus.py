#!/usr/bin/env python3
"""Focus on a planet: sweep the EAF and score the planet's image on the rtmp
(video) page, coarse to fine, then park on the vertex.

Two metrics that do not need a star:
  size   equivalent diameter of the blob above half its peak (px) -- MINIMISE.
         Exposure-independent, monotonic over a huge range: a grossly
         defocused planet is a big donut, a focused one a small disc.
         Drives the COARSE search (thousands of steps).
  sharp  sum of squared gradients / flux^2 inside a box on the planet --
         MAXIMISE. Seeing scatters it, so each position takes --frames
         frames and keeps the best quarter (lucky imaging, same as the stack).
         Drives the FINE search (tens to hundreds of steps).

A third, when a moon sits in the frame: --moon X,Y gives a Gaussian FWHM at
that pixel (MINIMISE), the cleanest of the three.

Positions are visited interleaved (odd/even), the order reversed on the next
round, so a seeing trend cannot masquerade as a focus slope. The EAF is put
on the winner (parabola through the best three) before exit. EAF limits come
from daypipes (EAF_MIN / EAF_MAX in the environment).

    EAF_MIN=30000 EAF_MAX=95000 ASIAIR_HOST=... python3 -u focus/planetfocus.py \
        --lo 41540 --hi 75540 --step 2000 --frames 6 --metric size          # coarse, barlow
    python3 -u focus/planetfocus.py --pos 55000,55150,55300,55450,55600 --rounds 2 --metric sharp
"""
import argparse, math, os, sys, time
import numpy as np
from scipy import ndimage, optimize
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from daypipes import host, Pipes, log, save_png, move_to, focuser_pos

ap = argparse.ArgumentParser()
ap.add_argument("--pos", help="comma list of EAF positions")
ap.add_argument("--lo", type=int); ap.add_argument("--hi", type=int); ap.add_argument("--step", type=int)
ap.add_argument("--rounds", type=int, default=1)
ap.add_argument("--frames", type=int, default=6, help="frames per position; the best quarter by 'sharp' is kept")
ap.add_argument("--exp-ms", type=float, default=15.0)
ap.add_argument("--gain", type=int, default=350)
ap.add_argument("--roi", type=int, default=0, help="square readout ROI in sensor px (0 = the page's 1920x1080 window)")
ap.add_argument("--page", default="rtmp", choices=["rtmp", "preview"], help="preview = 16-bit bin 2 full field (for a faint gross-defocus donut)")
ap.add_argument("--bin", type=int, default=1)
ap.add_argument("--metric", default="size", choices=["size", "sharp", "moon"], help="which metric picks the vertex")
ap.add_argument("--moon", default=None, help="X,Y of a moon in the frame for the FWHM metric")
ap.add_argument("--park", type=int, default=None, help="force this position at the end instead of the winner")
ap.add_argument("--tag", default="pfocus")
ap.add_argument("--hold", action="store_true", help="re-centre the planet with a small goto when it drifts more than --recentre-px from the frame centre")
ap.add_argument("--recentre-px", type=float, default=250.0)
ap.add_argument("--jacobian", default="341,79,-104,362", help="px/arcmin at the current bin/pier side (default: barlow, bin 1, pier west)")
ap.add_argument("--hold-mode", default="goto", choices=["goto", "pulse"], help="pulse: no goto (for a register that cannot be trusted) -- Dec by 20x joystick pulses, RA east by pausing tracking, RA west by a pulse")
ap.add_argument("--east", default="0.239,0.971", help="--hold-mode pulse: sky east on the sensor (camangle.py)")
ap.add_argument("--arcsec-per-px", type=float, default=0.110, help="--hold-mode pulse: bin-1 scale")
ap.add_argument("--outdir", default="/Users/madsdorup/ASICAP/telemetry/planetfocus")
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)
positions = [int(x) for x in a.pos.split(",")] if a.pos else list(range(a.lo, a.hi + 1, a.step))
srt = sorted(positions)
order = (srt[::2] + srt[1::2][::-1]) if len(positions) > 2 else list(positions)
CHIP = (3840, 2160)

def blob(im):
    """Planet blob above half its peak: centroid, equivalent diameter, peak, flux.
    Works in the frame's own units (8-bit video or 16-bit preview): the blob must
    clear both a fixed floor and 8 sigma of the sky noise."""
    a8 = im.astype(np.float64)
    bg = float(np.median(a8[::5, ::5])); sig = max(1.0, 1.4826 * float(np.median(np.abs(a8[::5, ::5] - bg))))
    sm = ndimage.uniform_filter(a8, 5); pk = float(sm.max())
    if pk - bg < max(8.0 * sig, 12.0 if im.max() <= 255 else 300.0):
        return None
    m = sm >= bg + 0.5 * (pk - bg)
    lab, n = ndimage.label(m)
    if n == 0:
        return None
    sizes = ndimage.sum(m, lab, range(1, n + 1)); i = int(np.argmax(sizes)) + 1
    ys, xs = np.nonzero(lab == i)
    area = len(xs); cx, cy = float(xs.mean()), float(ys.mean())
    flux = float((a8 - bg)[max(0, int(cy) - 400):int(cy) + 400, max(0, int(cx) - 400):int(cx) + 400].clip(0).sum())
    return dict(x=cx, y=cy, diam=2.0 * math.sqrt(area / math.pi), peak=pk - bg, area=area, flux=flux, bg=bg)

def sharp(im, b, half=None):
    a8 = im.astype(np.float64)
    half = half or int(max(60, b["diam"] * 1.2))
    h, w = a8.shape; cx, cy = int(b["x"]), int(b["y"])
    roi = a8[max(0, cy - half):min(h, cy + half), max(0, cx - half):min(w, cx + half)].astype(np.float64) - b["bg"]
    flux = float(roi.clip(0).sum())
    if flux <= 0:
        return 0.0
    gx = np.diff(roi, axis=1)[:-1, :]; gy = np.diff(roi, axis=0)[:, :-1]
    return float((gx * gx + gy * gy).sum()) / (flux * flux) * 1e6

def find_moon(im, b, rmin=90, rmax=600):
    """Brightest compact source between rmin and rmax px of the planet blob (Titan, normally)."""
    a8 = im.astype(np.float64); bg = float(np.median(a8[::5, ::5]))
    sig = max(1.0, 1.4826 * float(np.median(np.abs(a8[::5, ::5] - bg))))
    sm = ndimage.gaussian_filter(a8, 1.5)
    Y, X = np.mgrid[:a8.shape[0], :a8.shape[1]]
    rr = np.hypot(X - b["x"], Y - b["y"])
    cand = (sm > bg + 8 * sig) & (rr > max(rmin, b["diam"] * 1.3)) & (rr < rmax)
    lab, n = ndimage.label(cand)
    if n == 0:
        return None
    best = None
    for i in range(1, n + 1):
        ys, xs = np.nonzero(lab == i)
        if len(xs) < 3 or len(xs) > 2000:
            continue
        pk = float(sm[ys, xs].max())
        if best is None or pk > best[0]:
            j = int(np.argmax(sm[ys, xs])); best = (pk, int(xs[j]), int(ys[j]))
    return None if best is None else (best[1], best[2])

def moon_fwhm(im, gx, gy, r=14):
    a8 = (im if im.max() <= 255 else im / 256.0).astype(np.float64)
    bg = float(np.median(a8[::5, ::5]))
    sub = ndimage.gaussian_filter(a8, 1.5)[gy - r:gy + r + 1, gx - r:gx + r + 1]
    dy, dx = np.unravel_index(np.argmax(sub), sub.shape); cx, cy = gx - r + dx, gy - r + dy
    y0, y1, x0, x1 = cy - 12, cy + 13, cx - 12, cx + 13
    box = a8[y0:y1, x0:x1]; Y, X = np.mgrid[y0:y1, x0:x1]
    g = lambda p: (p[0] * np.exp(-((X - p[1]) ** 2 / (2 * p[3] ** 2) + (Y - p[2]) ** 2 / (2 * p[4] ** 2))) + p[5] - box).ravel()
    p, _ = optimize.leastsq(g, [box.max() - bg, cx, cy, 3, 3, bg])
    return 2.3548 * math.sqrt(abs(p[3] * p[4])), (float(p[1]), float(p[2]))

p = Pipes()
rows = []
JM = np.array([float(v) for v in a.jacobian.split(",")]).reshape(2, 2)
mt = None
def recentre_pulse(b, w, h):
    """Planet back to the centre without a goto (2026-09-26: register reset to 'home' by a power cut).
    Pier west: 'south' raises Dec, 'north' lowers it; 20x = 312"/s; tracking off moves the pointing east 15"/s."""
    from air_rpc import Air
    E = np.array([float(v) for v in a.east.split(",")]); E /= np.linalg.norm(E); N = np.array([-E[1], E[0]])
    d = np.array([b["x"] - w / 2.0, b["y"] - h / 2.0]) * (a.bin if a.page == "preview" else 1)
    east_as, north_as = float(d @ E) * a.arcsec_per_px, float(d @ N) * a.arcsec_per_px
    def mdo(fn):
        m = Air(host(), 4400)
        try: return fn(m)
        finally: m.close()
    def pulse(cmd, secs):
        def f(m):
            idx = m.call("scope_get_info", [])["result"]["slew_rate_index"]
            try: m.call("scope_set_slew_rate", [4]); m.call("scope_move", [cmd]); time.sleep(secs)
            finally: m.call("scope_move", ["none"]); m.call("scope_set_slew_rate", [idx])
        mdo(f)
    if abs(north_as) > 10:
        pulse("south" if north_as > 0 else "north", min(abs(north_as) / 312.0, 1.0))
    if east_as > 10:
        try: mdo(lambda m: m.call("scope_set_track_state", [False])); time.sleep(min(east_as / 15.0, 8.0))
        finally: mdo(lambda m: m.call("scope_set_track_state", [True]))
    elif east_as < -10:
        pulse("west", min(-east_as / 312.0, 1.0))
    time.sleep(0.8)
    log("  re-centred by pulses: planet was %.0f px off = %+.0f\" east %+.0f\" north" % (float(np.hypot(*d)), east_as, north_as))

def recentre(b, w, h):
    """One small goto so the planet stays in the window during a long sweep."""
    global mt
    if a.hold_mode == "pulse":
        return recentre_pulse(b, w, h)
    from mount import Mount
    try:
        mt.state()
    except Exception:
        mt = Mount(host())
    need = np.array([w / 2.0 - b["x"], h / 2.0 - b["y"]])
    corr = np.linalg.solve(JM, need)
    st = mt.state()
    ra = st["RA"] + corr[0] / (60.0 * 15.0 * math.cos(math.radians(st["Dec"]))); dec = st["Dec"] + corr[1] / 60.0
    mt.goto(ra, dec, wait=True, timeout=20)
    log("  re-centred: planet was %.0f px off -> RA %+.2f' Dec %+.2f'" % (float(np.hypot(*need)), corr[0], corr[1]))
try:
    p.setup(a.page, a.exp_ms / 1000.0, a.gain, a.bin if a.page == "preview" else 1)
    if a.roi and a.page == "rtmp":
        p.c("stop_exposure"); time.sleep(0.8)
        x0, y0 = (CHIP[0] - a.roi) // 2, (CHIP[1] - a.roi) // 2
        p.c("set_subframe", [{"x": x0, "y": y0, "width": a.roi, "height": a.roi}])
        log("subframe %s" % p.c("get_subframe"))
        p.c("set_control_value", ["Exposure", int(a.exp_ms * 1000)]); p.c("set_control_value", ["Gain", a.gain])
        p.s.air.drain_events(); p.last_sig = None; p.c("start_exposure", ["light"])
    start = focuser_pos(p)
    log("EAF at %d ; positions %s ; %d round(s) ; %d frames/pos ; metric %s" % (start, order, a.rounds, a.frames, a.metric))
    moon = a.moon if a.moon == "auto" else (tuple(int(v) for v in a.moon.split(",")) if a.moon else None)
    for r in range(a.rounds):
        seq = order if r % 2 == 0 else order[::-1]
        for pos in seq:
            got = move_to(p, pos)
            for _ in range(2):                          # frames that may predate the move
                p.grab(timeout=10)
            fr = []
            t0 = time.time()
            while len(fr) < a.frames and time.time() - t0 < 30:
                img, w, h, info = p.grab(timeout=10)
                if info.get("fresh"):
                    fr.append(img)
            bl = [blob(im) for im in fr]
            ok = [(im, b) for im, b in zip(fr, bl) if b]
            if not ok:
                log("r%d pos %6d: no planet in %d frames (max %.0f)" % (r, got, len(fr), max(float(im.max()) for im in fr) if fr else 0));
                rows.append(dict(round=r, pos=got, size=float("nan"), sharp=0.0, moon=float("nan"), n=0)); continue
            sh = [sharp(im, b) for im, b in ok]
            keep = sorted(range(len(ok)), key=lambda i: -sh[i])[:max(1, len(ok) // 4)] if a.metric != "moon" else list(range(len(ok)))
            size = float(np.median([ok[i][1]["diam"] for i in keep]))
            shp = float(np.median([sh[i] for i in keep]))
            mf = float("nan")
            if moon:
                try:
                    vals = []
                    for i in keep:
                        mpos = find_moon(ok[i][0], ok[i][1]) if moon == "auto" else moon
                        if mpos is None:
                            continue
                        f, _ = moon_fwhm(ok[i][0], *mpos); vals.append(f)
                    if vals:
                        mf = float(np.median(vals))
                    else:
                        log("  no moon found in the kept frames")
                except Exception as e:
                    log("  moon fit failed: %s" % e)
            b = ok[keep[0]][1]
            log("r%d pos %6d: %d frames, planet at (%.0f,%.0f) peak %.0f/255 | size %.1f px  sharp %.3f  moon FWHM %s" % (
                r, got, len(ok), b["x"], b["y"], b["peak"], size, shp, ("%.2f px" % mf) if mf == mf else "-"))
            rows.append(dict(round=r, pos=got, size=size, sharp=shp, moon=mf, n=len(ok)))
            save_png(ok[keep[0]][0], "%s/%s_r%d_p%d.png" % (a.outdir, a.tag, r, got), shrink=2 if w > 1000 else 1)
            if a.hold and math.hypot(b["x"] - w / 2.0, b["y"] - h / 2.0) > a.recentre_px:
                try:
                    recentre(b, w, h)
                except Exception as e:
                    log("  re-centre failed: %s" % e)
    log("=== summary (median over rounds) ===")
    byp = {}
    for row in rows:
        byp.setdefault(row["pos"], []).append(row)
    summ = []
    for pos in sorted(byp):
        rs = byp[pos]
        med = lambda k: float(np.nanmedian([x[k] for x in rs]))
        s = dict(pos=pos, size=med("size"), sharp=med("sharp"), moon=med("moon")); summ.append(s)
        log("  %6d  size %6.1f px   sharp %7.3f   moon %s" % (pos, s["size"], s["sharp"], ("%.2f" % s["moon"]) if s["moon"] == s["moon"] else "-"))
    key = a.metric
    valid = [s for s in summ if s[key] == s[key] and (key != "sharp" or s[key] > 0)]
    if not valid:
        raise RuntimeError("no valid measurements")
    best = (min if key in ("size", "moon") else max)(valid, key=lambda s: s[key])
    win = best["pos"]
    i = [s["pos"] for s in summ].index(win)
    if 0 < i < len(summ) - 1 and all(summ[j][key] == summ[j][key] for j in (i - 1, i + 1)):
        x = np.array([summ[j]["pos"] for j in (i - 1, i, i + 1)], float); y = np.array([summ[j][key] for j in (i - 1, i, i + 1)], float)
        c = np.polyfit(x, y, 2)
        if c[0] != 0:
            v = -c[1] / (2 * c[0])
            if min(x) <= v <= max(x):
                win = int(round(v))
        log("vertex by %s: best sample %d, parabola %d" % (key, best["pos"], win))
    else:
        log("best by %s is at the edge of the range (%d) -- extend the sweep that way" % (key, win))
    park = a.park if a.park is not None else win
    move_to(p, park)
    log("EAF parked at %d" % focuser_pos(p))
finally:
    p.close()
    try:
        if mt is not None: mt.close()
    except Exception: pass
    log("closed")
