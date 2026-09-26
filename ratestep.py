#!/usr/bin/env python3
"""Camera angle on the sky from a tracking-rate step, pointed at the Moon.

Lunar -> Sidereal -> Lunar while taking frames. The mount's lunar rate is 0.549"/s slower
in RA than sidereal (checked 2026-09-24 against the Jacobian's scale), so under sidereal
the Moon walks EAST through the field at that speed. The step in image velocity is
therefore sky-east on the sensor, its length checks the plate scale, and with the parallactic
angle q it gives the zenith direction -- which is what the ADC has to be lined up with.
No slew; the mount is always left on Lunar.

    ASIAIR_HOST=192.168.1.36 python3 -u ratestep.py --page preview --bin 1 --exp-ms 100 --every 5 --seg-lunar 30 --seg-sidereal 45 --adc-br=-21.3,14.2
    python3 ratestep.py --analyse telemetry/2026-09-26/ratestep/195900_ratestep.json --adc-br=-21.3,14.2
"""
import argparse, datetime as dt, json, math, os, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from moonreg import bandpass, measure_shift

ap = argparse.ArgumentParser()
ap.add_argument("--page", default="preview", choices=["preview", "focus"])
ap.add_argument("--bin", type=int, default=1)
ap.add_argument("--exp-ms", type=float, default=100.0); ap.add_argument("--gain", type=int, default=100)
ap.add_argument("--every", type=float, default=5.0, help="frame spacing, s (wall clock)")
ap.add_argument("--seg-lunar", type=float, default=30.0, help="seconds on Lunar before and after the sidereal segment")
ap.add_argument("--seg-sidereal", type=float, default=45.0)
ap.add_argument("--rate-diff", type=float, default=0.549, help="sidereal minus lunar tracking rate, arcsec/s of RA")
ap.add_argument("--arcsec-per-px", type=float, default=0.110, help="bin-1 sensor scale")
ap.add_argument("--jacobian", default="-23,299.8,-237.5,27.5", help="old bin-2 Jacobian (pier west 2026-09-16) for the comparison and the image parity")
ap.add_argument("--lat", type=float, default=55.689444); ap.add_argument("--lon", type=float, default=12.555278)
ap.add_argument("--adc-br", default=None, help="a measured B-minus-R offset 'dx,dy' (sensor px, from moonadc.py) to split into along/across the vertical")
ap.add_argument("--analyse", default=None, help="re-analyse a saved run record instead of taking frames")
a = ap.parse_args()

HERE = os.path.dirname(os.path.abspath(__file__))

def log(msg):
    print("%s  %s" % (time.strftime("%H:%M:%S"), msg), flush=True)

def track_mode(host, name=None):
    """Fresh 4400 socket per call: 4400 drops idle sockets, and a segment is 30-45 s long."""
    from air_rpc import Air
    m = Air(host, 4400)
    try:
        if name is not None:
            r = m.call("scope_set_track_mode", [name])
            log("scope_set_track_mode [%r] -> %s" % (name, r.get("result", r) if isinstance(r, dict) else r))
        r = m.call("scope_get_track_mode", [])
        r = r.get("result", r) if isinstance(r, dict) else r
        return r["list"][r["index"]]
    finally:
        m.close()

def take():
    from daypipes import Pipes, host
    h = host()
    outdir = os.path.join(HERE, "..", "telemetry", time.strftime("%Y-%m-%d"), "ratestep")
    os.makedirs(outdir, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    rec = dict(host=h, page=a.page, bin=a.bin, exp_ms=a.exp_ms, gain=a.gain, rows=[], switches=[])
    mode = track_mode(h)
    log("track mode at start: %s" % mode)
    if mode != "Lunar":
        mode = track_mode(h, "Lunar")
    p = Pipes(h)
    try:
        p.setup(a.page, a.exp_ms / 1000.0, a.gain, a.bin); p.grab(timeout=40)
        plan = [("Lunar", a.seg_lunar), ("Sidereal", a.seg_sidereal), ("Lunar", a.seg_lunar)]
        t0 = time.time(); k = 0
        for si, (want, dur) in enumerate(plan):
            if si:
                ts = time.time()
                got = track_mode(h, want)
                rec["switches"].append(dict(t_rel=ts - t0, mode=got))
                log("SWITCH at t=%.1fs -> %s" % (ts - t0, got))
            seg_end = time.time() + dur
            while time.time() < seg_end:
                tn = time.time()
                img, w, hh, info = p.grab(timeout=40)
                tm = tn + a.exp_ms / 2000.0 + 0.3                         # ~mid-exposure: start_exposure goes out ~0.3 s into grab()
                fn = os.path.join(outdir, "%s_%02d_%s.npy" % (stamp, k, want.lower()))
                np.save(fn, img.astype(np.uint16))
                rec["rows"].append(dict(npy=fn, t_rel=tm - t0, mode=want))
                log("frame %2d t=%6.1fs %-8s %dx%d fresh=%s p99.5 %.0f  (%.1fs)" % (k, tm - t0, want, w, hh, info.get("fresh"), np.percentile(img, 99.5), time.time() - tn))
                k += 1
                time.sleep(max(0.0, a.every - (time.time() - tn)))
    finally:
        try: p.close()
        except Exception: pass
        try: log("track mode restored: %s" % track_mode(h, "Lunar"))
        except Exception as e: log("!! could not restore Lunar: %s -- set it by hand" % e)
    fn = os.path.join(outdir, "%s_ratestep.json" % stamp)
    rec["utc_mid"] = dt.datetime.now(dt.timezone.utc).isoformat()
    json.dump(rec, open(fn, "w"), indent=1)
    log("run record -> %s" % fn)
    return rec

def green(img):
    return (img[0::2, 1::2].astype(np.float64) + img[1::2, 0::2]) / 2

def prep(g, hi=25.0):
    sm = ndimage.gaussian_filter(g, hi); disc = sm > 0.2 * np.percentile(sm, 99)
    wt = np.ones_like(g) if disc.mean() > 0.98 else ndimage.gaussian_filter(ndimage.binary_dilation(disc, iterations=int(hi)).astype(np.float64), hi / 3)
    return bandpass(g, 2.0, hi) * wt

def analyse(rec):
    rows = rec["rows"]; k2s = 2 * rec["bin"]                                  # plane px -> sensor px
    P = [prep(green(np.load(r["npy"]))) for r in rows]
    t = np.array([r["t_rel"] for r in rows])
    pos = [(0.0, 0.0)]
    for k in range(1, len(P)):
        dx, dy, q = measure_shift(P[k - 1], P[k], prepped=True)
        pos.append((pos[-1][0] + dx * k2s, pos[-1][1] + dy * k2s))
        print("  t=%6.1fs %-8s step %+7.2f %+7.2f  pos %+8.1f %+8.1f sensor px (q %.3f)" % (t[k], rows[k]["mode"], dx * k2s, dy * k2s, pos[-1][0], pos[-1][1], q))
    pos = np.array(pos)
    sw = [s["t_rel"] for s in rec["switches"]]
    t1, t2 = sw[0], sw[1]
    S = np.clip(t, t1, t2) - t1                                               # seconds spent on sidereal so far
    A = np.column_stack([np.ones_like(t), t, S])
    cx, rx, *_ = np.linalg.lstsq(A, pos[:, 0], rcond=None); cy, ry, *_ = np.linalg.lstsq(A, pos[:, 1], rcond=None)
    res = pos - A @ np.column_stack([cx, cy])
    dv = np.array([cx[2], cy[2]]); v0 = np.array([cx[1], cy[1]])
    exp_len = a.rate_diff / a.arcsec_per_px
    print("drift on Lunar: %+.2f %+.2f sensor px/s = %.3f\"/s ; fit residual sd %.1f/%.1f px" % (v0[0], v0[1], np.hypot(*v0) * a.arcsec_per_px, res[:, 0].std(), res[:, 1].std()))
    print("velocity step Sidereal-Lunar: %+.2f %+.2f sensor px/s  |%.2f| px/s  (expected %.2f at %.3f\"/px -> scale from this step %.4f\"/px)" % (
        dv[0], dv[1], np.hypot(*dv), exp_len, a.arcsec_per_px, a.rate_diff / np.hypot(*dv)))
    east = dv / np.hypot(*dv)
    J = np.array([float(v) for v in a.jacobian.split(",")]).reshape(2, 2)
    e_old = -J[:, 0] / np.linalg.norm(J[:, 0]); n_old = -J[:, 1] / np.linalg.norm(J[:, 1])
    parity = np.sign(e_old[0] * n_old[1] - e_old[1] * n_old[0])               # a rotation of the camera keeps this
    north = parity * np.array([-east[1], east[0]])
    rot = math.degrees(math.atan2(e_old[0] * east[1] - e_old[1] * east[0], float(np.dot(e_old, east))))
    print("sky EAST on the sensor now: (%+.3f, %+.3f) ; 2026-09-16 Jacobian said (%+.3f, %+.3f) -> camera rotated %+.1f deg since (+ = x toward y)" % (east[0], east[1], e_old[0], e_old[1], rot))
    print("sky NORTH on the sensor now: (%+.3f, %+.3f)" % tuple(north))
    from moonephem import moon
    tm = dt.datetime.fromisoformat(rec["utc_mid"]) - dt.timedelta(seconds=float(t[-1] - t.mean()))
    mm = moon(tm, a.lat, a.lon)
    up = math.sin(math.radians(mm["q"])) * east + math.cos(math.radians(mm["q"])) * north
    print("Moon alt %.2f az %.2f q %+.1f at mid-run -> ZENITH on the sensor: (%+.3f, %+.3f)" % (mm["alt"], mm["az"], mm["q"], up[0], up[1]))
    out = dict(east=east.tolist(), north=north.tolist(), up=up.tolist(), q=mm["q"], alt=mm["alt"], rot_deg=rot, dv=dv.tolist(), scale=a.rate_diff / np.hypot(*dv))
    if a.adc_br:
        br = np.array([float(v) for v in a.adc_br.split(",")])
        along = float(np.dot(br, up)); perp = float(up[0] * br[1] - up[1] * br[0])
        ang = math.degrees(math.atan2(perp, along))
        print("B-R (%+.1f, %+.1f) px: %+.1f px along the zenith (%+.2f\"), %+.1f px across (%+.2f\"); it points %+.0f deg from straight up" % (
            br[0], br[1], along, along * a.arcsec_per_px, perp, perp * a.arcsec_per_px, ang))
        out.update(br_along=along, br_perp=perp, br_angle=ang)
    return out

rec = json.load(open(a.analyse)) if a.analyse else take()
print(json.dumps(analyse(rec)))
