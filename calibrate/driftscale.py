#!/usr/bin/env python3
"""Plate scale (and so focal length) from the Moon's drift with tracking OFF.

    ASIAIR_HOST=192.168.1.36 python3 -u calibrate/driftscale.py --drift-s 60 --exp-ms 10 --gain 220

With tracking off the mount is fixed to the ground, so the image slides at the
Moon's apparent (refracted) speed in the alt/az frame -- ~14.3"/s, known from
the ephemeris and the sidereal rate to ~0.05%. Focus-page frames (1:1 crop,
~2/s) are time-stamped at their start_exposure, the static pattern (dust, hot
pixels) is removed with a temporal median, every pair at lags 1-8 is
phase-correlated, the positions are solved from all pairs at once, and position
vs time is fitted with a straight line. Tracking goes back on (Lunar) and the
mount returns to the starting register position in a finally block; the 4400
socket gets a keepalive every 4 s (it idle-drops at ~15 s and once left
tracking OFF).

The AM5N rings for ~10 s after tracking stops (78 / 74 / 80 px/s over 3-6 /
6-12 / 12-20 s, the same in every run, 2026-09-27), so --skip-s defaults to 10
and a run needs ~60 s: 10-20 s runs read 1-2% high. Two 60 s runs agreed to
0.7% (0.1862, 0.1875"/px). Do not cross-check with small RA gotos: RA is
quantized to 1 s of time on this mount (see gotoscale.py).

scale = apparent speed ("/s) / image speed (bin-1 px/s); F = 206265 * 2.9 um / scale.
The slide direction is sky-west (plus the vertical component of the diurnal
motion) on the sensor -- the camera angle comes free.

--analyse RUN.npz re-runs the analysis on a saved run.
"""
import argparse, datetime as dt, json, math, os, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import moonephem as me
from moonreg import bandpass, measure_shift

ap = argparse.ArgumentParser()
ap.add_argument("--drift-s", type=float, default=12.0, help="seconds of tracking-off frames")
ap.add_argument("--pre-frames", type=int, default=3, help="frames with tracking on before the pause")
ap.add_argument("--skip-s", type=float, default=10.0, help="ignore frames this soon after tracking-off: the AM5N rings for ~10 s after stopping (78/74/80 px/s in 3-6/6-12/12-20 s, 2026-09-27)")
ap.add_argument("--exp-ms", type=float, default=10.0); ap.add_argument("--gain", type=int, default=220)
ap.add_argument("--bin", type=int, default=1)
ap.add_argument("--pixel-um", type=float, default=2.9)
ap.add_argument("--lat", type=float, default=55.689444); ap.add_argument("--lon", type=float, default=12.555278)
ap.add_argument("--temp-c", type=float, default=12.0); ap.add_argument("--pressure-hpa", type=float, default=1013.0)
ap.add_argument("--no-return", action="store_true", help="do not goto back to the start position afterwards")
ap.add_argument("--analyse", default=None, help="re-analyse a saved .npz run instead of taking frames")
ap.add_argument("--outdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "telemetry", time.strftime("%Y-%m-%d"), "driftscale"))
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)
D2R = math.pi / 180


def log(s):
    print("%s  %s" % (time.strftime("%H:%M:%S"), s), flush=True)


def green(img):
    img = img.astype(np.float32)
    return 0.5 * (img[0::2, 1::2] + img[1::2, 0::2])


def refraction_arcmin(h_true):
    """Saemundsson, true altitude in degrees -> refraction in arcmin, scaled to T/P."""
    R = 1.02 / math.tan((h_true + 10.3 / (h_true + 5.11)) * D2R)
    return R * (a.pressure_hpa / 1010.0) * (283.0 / (273.0 + a.temp_c))


def apparent_altaz(t):
    m = me.moon(t, a.lat, a.lon)
    return m["alt"] + refraction_arcmin(m["alt"]) / 60.0, m["az"], m


def sep_deg(h1, z1, h2, z2):
    h1, z1, h2, z2 = [v * D2R for v in (h1, z1, h2, z2)]
    c = math.sin(h1) * math.sin(h2) + math.cos(h1) * math.cos(h2) * math.cos(z1 - z2)
    return math.acos(min(1.0, c)) / D2R


def expected_speed(t_mid, half=5.0):
    t1 = t_mid - dt.timedelta(seconds=half); t2 = t_mid + dt.timedelta(seconds=half)
    h1, z1, _ = apparent_altaz(t1); h2, z2, m = apparent_altaz(t2)
    v = sep_deg(h1, z1, h2, z2) * 3600 / (2 * half)
    # geometric (unrefracted) speed for the record
    m1 = me.moon(t1, a.lat, a.lon); m2 = me.moon(t2, a.lat, a.lon)
    v0 = sep_deg(m1["alt"], m1["az"], m2["alt"], m2["az"]) * 3600 / (2 * half)
    # direction of motion on the sky as a position angle (N through E): via RA/Dec of the fixed-ground frame
    return v, v0, m


def analyse(ts, frames, t_off, meta):
    ts = np.asarray(ts); t_off = float(t_off)
    use = np.nonzero(ts > t_off + a.skip_s)[0]
    if use.size < 5:
        sys.exit("only %d tracking-off frames after the skip -- nothing to fit" % use.size)
    G = np.array([green(frames[i]) for i in use], dtype=np.float32)
    med = np.median(G, axis=0)                               # static pattern: the Moon moves, dust and hot pixels do not
    P = [bandpass(g - med, 1.5, 20.0) for g in G]
    # network adjustment: every pair at lags 1..8 constrains p[j] - p[k]; least squares for all p
    # (a plain cumulative sum of lag-1 shifts random-walks)
    n_ = len(P); obs = []; qs = []
    for L in range(1, 9):
        for k in range(n_ - L):
            dx, dy, q = measure_shift(P[k], P[k + L], prepped=True)
            qs.append(q)
            if q > 0.3:
                obs.append((k, k + L, dx, dy))
    A = np.zeros((len(obs) + 1, n_)); bx = np.zeros(len(obs) + 1); by = np.zeros(len(obs) + 1)
    for i, (k, j, dx, dy) in enumerate(obs):
        A[i, j] = 1; A[i, k] = -1; bx[i] = dx; by[i] = dy
    A[-1, 0] = 1
    pos = np.column_stack([np.linalg.lstsq(A, bx, rcond=None)[0], np.linalg.lstsq(A, by, rcond=None)[0]]) * 2 * a.bin
    tt = ts[use] - ts[use][0]
    fx = np.polyfit(tt, pos[:, 0], 1); fy = np.polyfit(tt, pos[:, 1], 1)
    rx = pos[:, 0] - np.polyval(fx, tt); ry = pos[:, 1] - np.polyval(fy, tt)
    vx, vy = fx[0], fy[0]
    v = math.hypot(vx, vy)
    # slope uncertainty from the residual scatter
    n = tt.size; sxx = ((tt - tt.mean()) ** 2).sum()
    sig = math.sqrt((rx ** 2 + ry ** 2).sum() / max(2 * n - 4, 1))
    ev = sig / math.sqrt(sxx)
    lagv = {}
    for lag in (2, 4, 6, 8):
        if len(P) > lag + 2:
            sp = []
            for k in range(len(P) - lag):
                dx, dy, q = measure_shift(P[k], P[k + lag], prepped=True)
                sp.append(math.hypot(dx, dy) * 2 * a.bin / (tt[k + lag] - tt[k]))
            lagv[lag] = (float(np.mean(sp)), float(np.std(sp) / math.sqrt(len(sp))), len(sp))
    t_mid = dt.datetime.fromtimestamp(float(ts[use].mean()), dt.timezone.utc)
    vs, vs0, m = expected_speed(t_mid)
    scale = vs / v
    F = 206264.806 * a.pixel_um * 1e-3 / scale
    log("fit over %d frames, %.1f s: image velocity (%+.2f, %+.2f) bin-1 px/s = %.3f px/s +- %.3f (%.2f%%)" % (n, tt[-1], vx, vy, v, ev, 100 * ev / v))
    log("  residuals rms %.2f px (x %.2f, y %.2f); correlation quality min %.3f median %.3f" % (sig, rx.std(), ry.std(), min(qs), float(np.median(qs))))
    log("  Moon alt %.2f az %.2f: apparent speed %.4f\"/s (unrefracted %.4f\"/s; refraction %.3f%%)" % (m["alt"], m["az"], vs, vs0, 100 * (vs / vs0 - 1)))
    log("SCALE %.5f \"/px at bin 1 (+- %.2f%%)  ->  focal length %.0f mm  (f/%.1f on 203.2 mm)  field %.2f' x %.2f'" %
        (scale, 100 * ev / v, F, F / 203.2, 3840 * scale / 60, 2160 * scale / 60))
    for lag, (mv, se, nn) in lagv.items():
        log("  lag %d pairs: %.3f px/s +- %.3f (n %d) -> %.5f \"/px" % (lag, mv, se, nn, vs / mv))
    d = np.array([vx, vy]) / v
    log("  slide direction on the sensor (unit, bin-1 x right / y down): (%+.3f, %+.3f)" % (d[0], d[1]))
    for i, k in enumerate(use):
        log("    t %6.2f s  pos (%+8.1f, %+8.1f)  resid (%+5.1f, %+5.1f)" % (tt[i], pos[i, 0], pos[i, 1], rx[i], ry[i]))
    return dict(scale=scale, focal_mm=F, v_px_s=v, v_err=ev, vx=vx, vy=vy, v_arcsec_s=vs, v_unrefracted=vs0, n=int(n), span_s=float(tt[-1]),
                resid_rms=sig, alt=m["alt"], az=m["az"], dir=[float(d[0]), float(d[1])], **meta)


if a.analyse:
    z = np.load(a.analyse, allow_pickle=True)
    r = analyse(z["ts"], z["frames"], float(z["t_off"]), json.loads(str(z["meta"])))
    sys.exit(0)

from daypipes import host, Pipes
from mount import Mount

H = host()
p = Pipes(); mnt = Mount(H)
ts, frames = [], []
t_off = None
st0 = mnt.state()
meta = dict(ra0=st0["RA"], dec0=st0["Dec"], pier=st0.get("pier_side"), exp_ms=a.exp_ms, gain=a.gain)
log("start register RA %.4f Dec %+.4f Alt %.2f Az %.2f track %s pier %s" % (st0["RA"], st0["Dec"], st0["Alt"], st0["Az"], st0["is_enable_track"], st0.get("pier_side")))
try:
    fi = p.c("get_focuser_info"); meta["eaf"] = fi.get("position"); meta["eaf_temp"] = fi.get("temperature")
    log("EAF %s at %s C" % (fi.get("position"), fi.get("temperature")))
    p.setup("focus", a.exp_ms / 1000.0, a.gain, a.bin)

    def shot():
        p.s.air.drain_events()
        t0 = time.time(); p.c("start_exposure"); t1 = time.time()
        time.sleep(max(a.exp_ms / 1000.0, 0.05))
        img, w, h, depth, sig, hdr = p._download()
        k = 0
        while sig == p.last_sig and k < 200:
            k += 1; time.sleep(0.05)
            img, w, h, depth, sig, hdr = p._download()
        p.last_sig = sig
        ts.append(0.5 * (t0 + t1) + a.exp_ms / 2000.0); frames.append(np.asarray(img))
        return t1 - t0

    shot()                                                   # discard: may predate the setup
    ts.clear(); frames.clear()
    for i in range(a.pre_frames):
        rtt = shot(); log("  pre %d  rpc %.0f ms  p99.5 %.0f" % (i, 1000 * rtt, np.percentile(frames[-1], 99.5)))
    mnt._r("scope_set_track_state", [False]); t_off = time.time()
    log("TRACKING OFF -- sliding for %.0f s" % a.drift_s)
    last_ka = time.time()
    while time.time() - t_off < a.drift_s:
        if time.time() - last_ka > 4.0:                      # 4400 drops a socket idle for ~15 s
            mnt.state(); last_ka = time.time()
        rtt = shot()
        log("  [%5.2f s] frame %d  rpc %.0f ms" % (ts[-1] - t_off, len(frames), 1000 * rtt))
finally:
    try:
        try:
            mnt._r("scope_set_track_state", [True])
        except Exception as e:
            log("mount socket dead (%s) -- reconnecting to restore tracking" % e)
            try: mnt.close()
            except Exception: pass
            mnt = Mount(H); mnt._r("scope_set_track_state", [True])
        mnt._r("scope_set_track_mode", ["Lunar"])
        s = mnt.state(); log("tracking back ON: %s, mode %s" % (s["is_enable_track"], mnt._r("scope_get_track_mode").get("index")))
        if not a.no_return:
            mnt.goto(meta["ra0"], meta["dec0"], timeout=60)
            s = mnt.state(); log("returned to RA %.4f Dec %+.4f" % (s["RA"], s["Dec"]))
    except Exception as e:
        log("!! RESTORE FAILED: %s -- re-enable tracking by hand" % e)
    try: p.close()
    except Exception: pass
    mnt.close()

stamp = time.strftime("%H%M%S")
fn = os.path.join(a.outdir, "%s_drift.npz" % stamp)
np.savez_compressed(fn, ts=np.array(ts), frames=np.array(frames), t_off=t_off, meta=json.dumps(meta))
log("saved %d frames to %s" % (len(frames), fn))
r = analyse(ts, frames, t_off, meta)
json.dump(r, open(os.path.join(a.outdir, "%s_drift.json" % stamp), "w"), indent=1)
