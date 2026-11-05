#!/usr/bin/env python3
"""Hold a small bright target (planetary nebula, Cat's Eye, NGC 7027) on the focus page and
save every short sub -- planet-style lucky imaging of a deep-sky object.

Focus page, bin 1 (the 1:1 centre crop, 16-bit), fixed exposure/gain. Every frame: the
target's centroid (brightest smoothed blob); a crop around it is saved as .npy, plus a full
crop every --full-every frames. When the target sits more than --deadband arcsec off centre
and the last correction is --gap s old, slowpulse.Nudger moves the pointing by --hold-gain of
the error (1x/4x mount-timed pulses), and the next frame is discarded. Every minute: tracking
(re-enabled if off), pier side (a change stops the run), input volts.

Stops after --minutes, after --lost frames in a row without the target, or on SIGTERM
(finishes the move first). Writes <outdir>/<name>_<HHMMSS>/{frames/, hold.csv, run.json}.

    ASIAIR_HOST=192.168.1.36 python3 -u capture/pnhold.py --name ngc7027 --exp 0.5 --gain 350 \\
        --east=-0.664,0.747 --minutes 30
"""
import argparse, csv, json, os, signal, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from air_rpc import Air
from daypipes import Pipes, host
from slowpulse import Nudger

ap = argparse.ArgumentParser()
ap.add_argument("--name", required=True)
ap.add_argument("--exp", type=float, default=0.5, help="seconds")
ap.add_argument("--gain", type=int, default=350)
ap.add_argument("--east", required=True, help="sky east on the sensor 'x,y' (from a solve or camangle)")
ap.add_argument("--arcsec-per-px", type=float, default=0.1866, help="bin-1 scale")
ap.add_argument("--minutes", type=float, default=30.0)
ap.add_argument("--crop", type=int, default=512, help="saved crop size around the target, px")
ap.add_argument("--full-every", type=int, default=100, help="also save the whole focus frame every N frames")
ap.add_argument("--deadband", type=float, default=6.0, help="arcsec")
ap.add_argument("--gap", type=float, default=6.0, help="min seconds between corrections")
ap.add_argument("--hold-gain", type=float, default=0.7, help="fraction of the error corrected per nudge")
ap.add_argument("--min-peak", type=float, default=1500.0, help="target present if the smoothed peak is this many ADU over the sky")
ap.add_argument("--lost", type=int, default=15, help="stop after this many frames in a row without the target")
ap.add_argument("--outdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "telemetry", time.strftime("%Y-%m-%d"), "pnhold"))
a = ap.parse_args()

E = np.array([float(v) for v in a.east.split(",")]); E /= np.linalg.norm(E); N = np.array([-E[1], E[0]])
T0 = time.time()
def log(s): print("%s %+7.1fs  %s" % (time.strftime("%H:%M:%S"), time.time() - T0, s), flush=True)
def _term(signum, _f): raise SystemExit(128 + signum)
signal.signal(signal.SIGTERM, _term)

run = os.path.join(a.outdir, "%s_%s" % (a.name, time.strftime("%H%M%S")))
os.makedirs(os.path.join(run, "frames"), exist_ok=True)
json.dump(vars(a) | {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "page": "focus", "bin": 1},
          open(os.path.join(run, "run.json"), "w"), indent=1)


def mount_info():
    m = Air(host(), 4400, timeout=10)
    try:
        return m.call("scope_get_info", [], timeout=10)["result"]
    finally:
        m.close()


def find(img):
    """Centroid of the brightest smoothed blob, or None."""
    f = img.astype(np.float32); bg = float(np.median(f[::4, ::4]))
    sm = ndimage.gaussian_filter(f, 3); pk = float(sm.max())
    if pk - bg < a.min_peak:
        return None, bg, pk - bg
    lab, n = ndimage.label(sm > bg + 0.25 * (pk - bg))
    k = int(lab[np.unravel_index(int(np.argmax(sm)), sm.shape)])
    m = lab == k; w = np.clip(f - bg, 0, None) * m; s = w.sum()
    ys, xs = np.nonzero(m)
    return (float((w[ys, xs] * xs).sum() / s), float((w[ys, xs] * ys).sum() / s), float(s)), bg, pk - bg


info = mount_info(); pier0 = info.get("pier_side")
log("start: pier %s, tracking %s, %.2f V, RA %.4f Dec %.4f ; %s -> %s" % (
    pier0, info.get("is_enable_track"), info.get("input_voltage", 0) / 1000, info["RA"], info["Dec"], a.name, run))
nudger = Nudger(host(), dec_up="auto", log=log)
p = Pipes()
fcsv = open(os.path.join(run, "hold.csv"), "w", newline="")
wr = csv.writer(fcsv); wr.writerow(["t", "frame", "saved", "cx", "cy", "err_e_as", "err_n_as", "peak", "flux", "bg", "nudge_e_as", "nudge_n_as"])
n = saved = lost = corrections = 0; last_corr = 0.0; last_status = time.time(); skip = 1; errs = []
stop = "time"
try:
    p.setup("focus", a.exp, a.gain, 1)
    while time.time() - T0 < a.minutes * 60:
        img, w, h, _ = p.grab(timeout=max(20.0, 10 * a.exp)); n += 1
        if skip:                                            # exposed during a move: may be smeared
            skip -= 1; continue
        hit, bg, pk = find(img)
        if hit is None:
            lost += 1
            log("frame %d: target NOT found (peak %.0f over sky), %d in a row" % (n, pk, lost))
            wr.writerow([round(time.time() - T0, 2), n, "", "", "", "", "", round(pk), "", round(bg), "", ""])
            if lost >= a.lost:
                stop = "lost"; break
            continue
        lost = 0
        cx, cy, flux = hit
        d = (np.array([cx, cy]) - [w / 2.0, h / 2.0]) * a.arcsec_per_px
        ee, en = float(d @ E), float(d @ N); errs.append(np.hypot(ee, en))
        r = a.crop // 2; x0 = int(min(max(cx - r, 0), w - a.crop)); y0 = int(min(max(cy - r, 0), h - a.crop))
        np.save(os.path.join(run, "frames", "%s_%05d_x%d_y%d.npy" % (a.name, n, x0, y0)), img[y0:y0 + a.crop, x0:x0 + a.crop].astype(np.uint16))
        if n % a.full_every == 0:
            np.save(os.path.join(run, "frames", "%s_%05d_full.npy" % (a.name, n)), img.astype(np.uint16))
        saved += 1
        ne = nn = 0.0
        if np.hypot(ee, en) > a.deadband and time.time() - last_corr > a.gap:
            ne, nn = a.hold_gain * ee, a.hold_gain * en
            done = nudger.nudge(ne, nn); corrections += 1; last_corr = time.time(); skip = 1
            log("frame %d: off %+.1f\" E %+.1f\" N -> nudged %s" % (n, ee, en, ", ".join("%s %.0f\"" % (c, amt) for c, amt, _, _ in done) or "nothing"))
        wr.writerow([round(time.time() - T0, 2), n, saved, round(cx, 1), round(cy, 1), round(ee, 1), round(en, 1), round(pk), round(flux), round(bg), round(ne, 1), round(nn, 1)])
        if n % 20 == 0:
            fcsv.flush()
            last = errs[-20:]
            log("frame %d: %d saved, %d corrections ; offset median %.1f\" max %.1f\" (last 20) ; peak %.0f ADU over sky %.0f" % (
                n, saved, corrections, float(np.median(last)), float(np.max(last)), pk, bg))
        if time.time() - last_status > 60:
            last_status = time.time()
            info = mount_info()
            if not info.get("is_enable_track"):
                log("TRACKING WAS OFF -- re-enabling")
                m = Air(host(), 4400, timeout=10)
                try: m.call("scope_set_track_state", [True], timeout=10)
                finally: m.close()
            if info.get("pier_side") != pier0:
                log("PIER CHANGED %s -> %s: stopping" % (pier0, info.get("pier_side"))); stop = "pier"; break
            log("status: tracking %s, pier %s, %.2f V, alt %.1f" % (info.get("is_enable_track"), info.get("pier_side"), info.get("input_voltage", 0) / 1000, info.get("Alt", float("nan"))))
except SystemExit:
    stop = "signal"; raise
finally:
    fcsv.close()
    try: p.close()
    except Exception: pass
    try:
        m = Air(host(), 4400, timeout=10); m.call("scope_move", ["none"], timeout=10); m.close()
    except Exception as ex:
        log("final scope_move none failed: %s" % ex)
    log("STOP (%s): %d frames, %d saved, %d corrections, offset median %.1f\" -> %s" % (
        stop, n, saved, corrections, float(np.median(errs)) if errs else float("nan"), run))
