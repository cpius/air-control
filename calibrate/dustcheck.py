#!/usr/bin/env python3
"""Dust-shadow check on the Moon: are the window motes gone after a cleaning?

Dust near the sensor casts dark donuts FIXED on the sensor; lunar features move. So take
frames with the scene slid between them (tracking paused --pause-s each time: the Moon moves
~136 px/s sky-west at 0.110"/px), flat-field each, and take the per-pixel median: the Moon washes
out, the dust stays. Then measure the ring depth at the known mote positions and search for new
ones with a matched annulus filter. The pause direction also gives sky-west on the sensor, i.e.
the camera angle (the camera may have been re-seated). Tracking is always re-enabled, on Lunar.

    ASIAIR_HOST=192.168.1.36 python3 -u calibrate/dustcheck.py --frames 5 --pause-s 2.5 --ref telemetry/2026-09-26/moonlook/201154_02_adclive_preview_bin1_100ms_g100.npy
    python3 calibrate/dustcheck.py --npy telemetry/2026-09-26/dustcheck/*_dust_*.npy          # re-analyse
"""
import argparse, glob, json, math, os, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from moonreg import bandpass, measure_shift

HERE = os.path.dirname(os.path.abspath(__file__))
# 2026-09-26 20:11, before cleaning: ten motes, green-plane coords (x2 = sensor px), 116 px outer diameter
KNOWN = [(1416, 168), (1723, 294), (1518, 309), (1180, 711), (983, 661), (848, 418), (837, 188), (743, 537), (958, 444), (1579, 916)]
BEFORE = [os.path.join(HERE, "..", "..", "telemetry", "2026-09-26", f) for f in
          ("moonlook/195405_00_adclive_preview_bin1_100ms_g100.npy", "ratestep/195913_00_lunar.npy", "moonlook/201154_02_adclive_preview_bin1_100ms_g100.npy")]

ap = argparse.ArgumentParser()
ap.add_argument("--frames", type=int, default=5)
ap.add_argument("--pause-s", type=float, default=2.5, help="tracking off between frames, s (slides the Moon ~136 px/s)")
ap.add_argument("--exp-ms", type=float, default=100.0); ap.add_argument("--gain", type=int, default=100)
ap.add_argument("--npy", nargs="+", default=None, help="analyse these frames instead of taking new ones")
ap.add_argument("--before", nargs="+", default=BEFORE, help="frames from before the cleaning (same analysis, for comparison)")
ap.add_argument("--east", default="0.971,0.239", help="sky east on the sensor before the cleaning (ratestep 20:01)")
ap.add_argument("--arcsec-per-px", type=float, default=0.110)
a = ap.parse_args()

def log(s): print("%s  %s" % (time.strftime("%H:%M:%S"), s), flush=True)

def mount(host, fn):
    from air_rpc import Air
    m = Air(host, 4400)                 # fresh socket each time: 4400 drops idle sockets
    try: return fn(m)
    finally: m.close()

def res(r): return r.get("result", r) if isinstance(r, dict) else r

def take():
    from daypipes import Pipes, host
    h = host(); outdir = os.path.join(HERE, "..", "..", "telemetry", time.strftime("%Y-%m-%d"), "dustcheck"); os.makedirs(outdir, exist_ok=True)
    st = mount(h, lambda m: (res(m.call("scope_get_track_state", [])), res(m.call("scope_get_track_mode", []))))
    log("tracking %s, mode %s" % (st[0], st[1]["list"][st[1]["index"]]))
    p = Pipes(h); files = []; pauses = []
    try:
        p.setup("preview", a.exp_ms / 1000.0, a.gain, 1); p.grab(timeout=40)
        for i in range(a.frames):
            if i:
                t0 = time.time()
                mount(h, lambda m: m.call("scope_set_track_state", [False]))
                time.sleep(a.pause_s)
                mount(h, lambda m: m.call("scope_set_track_state", [True]))
                pauses.append(time.time() - t0)
                log("tracking paused %.2f s -> back on" % pauses[-1])
                time.sleep(0.5)
            img, w, hh, info = p.grab(timeout=40)
            fn = os.path.join(outdir, "%s_%02d_dust_preview_bin1_%gms_g%d.npy" % (time.strftime("%H%M%S"), i, a.exp_ms, a.gain))
            np.save(fn, img.astype(np.uint16)); files.append(fn)
            log("frame %d: %dx%d fresh=%s p1 %.0f p50 %.0f p99.5 %.0f -> %s" % (i, w, hh, info.get("fresh"), *np.percentile(img, [1, 50, 99.5]), os.path.basename(fn)))
    finally:
        try: p.close()
        except Exception: pass
        def restore(m):
            m.call("scope_set_track_state", [True])
            mo = res(m.call("scope_get_track_mode", []))
            if mo["list"][mo["index"]] != "Lunar": m.call("scope_set_track_mode", ["Lunar"])
            return res(m.call("scope_get_track_state", [])), res(m.call("scope_get_track_mode", []))
        try:
            s, mo = mount(h, restore); log("RESTORED: tracking %s, mode %s" % (s, mo["list"][mo["index"]]))
        except Exception as e:
            log("!! could not confirm tracking is back on (%s) -- CHECK THE MOUNT" % e)
    return files, pauses

def flat_green(f):
    r = np.load(f).astype(np.float64); g = (r[0::2, 1::2] + r[1::2, 0::2]) / 2
    disc = ndimage.gaussian_filter(g, 30) > 0.35 * np.percentile(g, 99)
    fl = g / np.maximum(ndimage.gaussian_filter(g, 60), 1.0)
    fl[~ndimage.binary_erosion(disc, iterations=80)] = np.nan
    return fl, float(disc.mean())

def static(files):
    F = [flat_green(f) for f in files]
    S = np.stack([x[0] for x in F]); med = np.nanmedian(S, axis=0); n = np.isfinite(S).sum(axis=0)
    med[n < (2 if len(files) <= 3 else 3)] = np.nan          # a median of 2 is a mean: lunar detail at half strength
    return med, [x[1] for x in F], F

yy, xx = np.mgrid[-44:45, -44:45]; rr = np.hypot(yy, xx)
RING = (rr >= 17) & (rr <= 27); OUT = (rr >= 33) & (rr <= 43)
def depth_at(hp, x, y):
    s = hp[y - 44:y + 45, x - 44:x + 45]
    if s.shape != rr.shape or not np.isfinite(s).all(): return float("nan")
    return float(s[RING].mean() - s[OUT].mean())

def analyse(med, tag):
    m = np.nan_to_num(med, nan=1.0); hp = m - ndimage.gaussian_filter(m, 40); hp[~np.isfinite(med)] = np.nan
    d = [depth_at(hp, x, y) for x, y in KNOWN]
    # matched filter for any donut: ring mean minus outer mean, everywhere
    k = RING / RING.sum() - OUT / OUT.sum()
    resp = ndimage.convolve(np.nan_to_num(hp), k, mode="nearest")
    valid = ndimage.binary_erosion(np.isfinite(med), iterations=45)
    resp[~valid] = 0
    mins = (resp == ndimage.minimum_filter(resp, 60)) & (resp < -0.012)
    ys, xs = np.nonzero(mins); order = np.argsort(resp[ys, xs])
    found = [(int(xs[i]), int(ys[i]), float(resp[ys[i], xs[i]])) for i in order[:25]]
    noise = float(np.nanstd(resp[valid]))
    print("%s: ring depth at the 10 known motes: %s  (median %+.2f%%) ; matched-filter noise %.2f%% over %.0f%% of the plane" % (
        tag, " ".join("%+.1f" % (100 * v) for v in d), 100 * np.nanmedian(d), 100 * noise, 100 * valid.mean()))
    print("%s: donut detections deeper than 1.2%%: %d%s" % (tag, len(found), "" if not found else " -> " + ", ".join("(%d,%d) %+.1f%%" % (x, y, 100 * v) for x, y, v in found[:12])))
    return d, found, hp

def to8(x, lo=-0.06, hi=0.06):
    return (np.clip((np.nan_to_num(x, nan=lo) - lo) / (hi - lo), 0, 1) * 255).astype(np.uint8)

if a.npy:
    files, pauses = sorted(sum([glob.glob(f) for f in a.npy], [])), []
else:
    files, pauses = take()
outdir = os.path.dirname(os.path.abspath(files[0]))
med_new, discs, F = static(files)
log("frames %d, disc fraction %s" % (len(files), ["%.0f%%" % (100 * d) for d in discs]))
# scene motion between frames -> sky-west on the sensor
P = [bandpass(np.nan_to_num(f[0], nan=1.0), 2, 30) * np.isfinite(f[0]) for f in F]
steps = []
for i in range(1, len(P)):
    dx, dy, q = measure_shift(P[i - 1], P[i], prepped=True); steps.append((2 * dx, 2 * dy, q))
    print("  scene step %d->%d: %+6.0f %+6.0f sensor px (q %.2f)" % (i - 1, i, 2 * dx, 2 * dy, q))
if steps:
    v = np.median(np.array([s[:2] for s in steps]), axis=0); east = -v / np.linalg.norm(v)
    e0 = np.array([float(t) for t in a.east.split(",")]); e0 /= np.linalg.norm(e0)
    rot = math.degrees(math.atan2(e0[0] * east[1] - e0[1] * east[0], float(e0 @ east)))
    rate = np.linalg.norm(v) / np.median(pauses) if pauses else float("nan")
    print("sky EAST on the sensor now (opposite the pause slide): (%+.3f, %+.3f) ; before the cleaning (%+.3f, %+.3f) -> camera now rotated %+.1f deg ; slide %.0f px per pause = %.0f px/s" % (
        east[0], east[1], e0[0], e0[1], rot, np.linalg.norm(v), rate))
med_old, _, _ = static(a.before)
d_old, f_old, hp_old = analyse(med_old, "BEFORE (19:54-20:11)")
d_new, f_new, hp_new = analyse(med_new, "NOW              ")
import cv2
stamp = os.path.basename(files[0])[:6]
both = np.hstack([to8(hp_old), np.full((hp_old.shape[0], 12), 255, np.uint8), to8(hp_new)])
cv2.imwrite(os.path.join(outdir, "%s_dust_before_after.png" % stamp), both)
log("before | after fixed-pattern image -> %s" % os.path.join(outdir, "%s_dust_before_after.png" % stamp))
