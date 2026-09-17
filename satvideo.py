#!/usr/bin/env python3
"""Record a planetary AVI with the Air's own recorder while HOLDING the planet
centred with small gotos.

Why gotos and not joystick pulses: a `scope_move` needs its `["none"]` stop to
arrive, and on the east balcony at -68 dBm that packet has been lost before
(see wifi-band-is-a-safety-issue). A goto of a few arcseconds stops by itself
and tonight completes in ~1 s. The frames smeared during a move are a handful
out of a thousand; the stacker's quality ranking drops them.

Sequence (order matters, measured 2026-09-02/10):
  preview page: bin 1 + full subframe   (set_subframe fails "out of limit" at bin 2)
  set_page(["rtmp"])                    (the video tab; resets the subframe, auto-starts)
  stop_exposure -> set_subframe ROI -> get_subframe to VERIFY -> exposure/gain
  start_exposure(["light"])             (first frame can take 30-70 s, then ~1-2 Hz on 4800)
  exposure test on the 8-bit frames     (planet peak into --peak-lo..--peak-hi of 255)
  start_record_avi ... hold loop ... stop_record_avi

    ASIAIR_HOST=192.168.1.35 python3 -u satvideo.py --seconds 30 --roi 640 --exp-ms 18 --gain 250
"""
import argparse, math, os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daypipes import host, Pipes, log, save_png
from mount import Mount

ap = argparse.ArgumentParser()
ap.add_argument("--seconds", type=float, default=30.0)
ap.add_argument("--roi", type=int, default=640, help="square readout ROI, sensor px, centred on the sensor")
ap.add_argument("--exp-ms", type=float, default=18.0)
ap.add_argument("--gain", type=int, default=250)
ap.add_argument("--peak-lo", type=int, default=150)
ap.add_argument("--peak-hi", type=int, default=210)
ap.add_argument("--jacobian", default="170.6,39.4,-51.8,180.8",
                help="bin-1 px per arcmin dx/dRA,dx/dDec,dy/dRA,dy/dDec for the CURRENT pier side (negated automatically if the first move goes the wrong way)")
ap.add_argument("--deadband", type=float, default=40.0, help="px: no correction inside this")
ap.add_argument("--min-gap", type=float, default=4.0, help="seconds between corrections")
ap.add_argument("--no-hold", action="store_true", help="record without corrections")
ap.add_argument("--min-peak", type=float, default=10.0, help="8-bit counts over sky that still count as the planet (thin cloud dims it)")
ap.add_argument("--exp-max", type=float, default=100.0, help="ms; the exposure test will not go longer than this")
ap.add_argument("--gain-max", type=int, default=450, help="raise the gain in steps of 50 up to this when the exposure cap is not enough")
ap.add_argument("--max-dim-exp", type=float, default=None, help="ms; if the exposure test needs more than this, treat it as cloud and do not record")
ap.add_argument("--max-dim-gain", type=int, default=None, help="if the exposure test needs more gain than this, treat it as cloud and do not record")
ap.add_argument("--outdir", default="/Users/madsdorup/ASICAP/telemetry/video")
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)
J = np.array([float(v) for v in a.jacobian.split(",")]).reshape(2, 2)
CHIP = (3840, 2160)

def planet(im):
    """Centroid of the brightest compact blob; 8-bit or 16-bit frame."""
    a8 = im if im.max() <= 255 else im / 256.0
    bg = float(np.median(a8[::5, ::5])); pk = float(a8.max())
    if pk - bg < a.min_peak:
        return None, "faint (peak %.0f over bg %.0f)" % (pk - bg, bg)
    thr = bg + 0.5 * (pk - bg)
    ys, xs = np.nonzero(a8 >= thr)
    if len(xs) < 30:
        return None, "small (%d px)" % len(xs)
    mx, my = np.median(xs), np.median(ys)
    m = (np.abs(xs - mx) < 200) & (np.abs(ys - my) < 200); xs, ys = xs[m], ys[m]
    wt = (a8[ys, xs] - bg)
    return (float((xs * wt).sum() / wt.sum()), float((ys * wt).sum() / wt.sum()), pk, int(len(xs))), "peak %.0f/255 area %d px bg %.0f" % (pk, len(xs), bg)

def fresh_frame(p, tries=8):
    """The rtmp page can stall 30-70 s on its first frame; Pipes caps a grab at 15 s."""
    for i in range(tries):
        img, w, h, info = p.grab(timeout=15)
        if info.get("fresh"):
            return img, w, h, info
        log("  ... no new frame yet (%d)" % (i + 1))
    raise RuntimeError("rtmp page is not delivering new frames")

m = Mount(host()); st = m.state(); st = m.state()
log("start: register RA %.4f Dec %.4f Alt %.1f Az %.1f pier=%s track=%s" % (st["RA"], st["Dec"], st["Alt"], st["Az"], st.get("pier_side"), st["is_enable_track"]))
if not st["is_enable_track"]:
    log("tracking was OFF -> %s" % m.set_tracking(True))
p = Pipes()
avi_events = []
try:
    p.setup("rtmp", a.exp_ms / 1000.0, a.gain, 1)         # bin 1 + full subframe on preview, then the video page
    p.c("stop_exposure"); time.sleep(0.8)
    x0, y0 = (CHIP[0] - a.roi) // 2, (CHIP[1] - a.roi) // 2
    want = {"x": x0, "y": y0, "width": a.roi, "height": a.roi}
    r = p.c("set_subframe", [want]); sf = p.c("get_subframe")
    if not (isinstance(sf, dict) and sf.get("width") == a.roi and sf.get("height") == a.roi):
        r = p.c("set_subframe", want); sf = p.c("get_subframe")
    log("subframe -> %s ; reads back %s" % (r, sf))
    if not (isinstance(sf, dict) and sf.get("width") == a.roi):
        raise RuntimeError("ROI not applied: %s" % sf)
    exp_ms = a.exp_ms; gain = a.gain
    p.c("set_control_value", ["Exposure", int(exp_ms * 1000)]); p.c("set_control_value", ["Gain", gain]); p.exp = exp_ms / 1000.0
    log("exposure %.1f ms gain %d ; bin %s" % (exp_ms, a.gain, p.c("get_camera_bin")))
    p.s.air.drain_events(); p.last_sig = None
    log("start_exposure(light) -> %s ; waiting for the first frame (can take 30-70 s)" % p.c("start_exposure", ["light"]))
    t0 = time.time()
    img, w, h, info = fresh_frame(p)
    log("first frame after %.0fs: %dx%d %d-bit" % (time.time() - t0, w, h, info["depth"]))

    # -- exposure test: put the planet's peak in the window ----------------
    for it in range(8):
        q, desc = planet(img)
        if q is None:
            log("exposure test %d: no planet in the frame (%s) -- taking another frame" % (it, desc))
            img, w, h, info = fresh_frame(p); continue
        log("exposure test %d: %.1f ms g%d -> planet at (%.0f,%.0f) %s" % (it, exp_ms, gain, q[0], q[1], desc))
        pk = q[2]
        if a.peak_lo <= pk <= a.peak_hi:
            break
        new = exp_ms * (min(2.5, 1.15 * a.peak_lo / pk) if pk < a.peak_lo else 0.9 * a.peak_hi / pk)
        new = max(2.0, min(a.exp_max, new))
        if abs(new - exp_ms) < 0.3:
            if pk < a.peak_lo and gain + 50 <= a.gain_max:
                gain += 50; p.c("set_control_value", ["Gain", gain]); log("  exposure capped at %.0f ms -> gain %d" % (exp_ms, gain))
                fresh_frame(p); img, w, h, info = fresh_frame(p); continue
            break
        exp_ms = new
        p.c("set_control_value", ["Exposure", int(exp_ms * 1000)]); p.exp = exp_ms / 1000.0
        log("  exposure -> %.1f ms" % exp_ms)
        fresh_frame(p); img, w, h, info = fresh_frame(p)
    save_png(img, "%s/%s_test_%.0fms_g%d.png" % (a.outdir, time.strftime("%H%M%S"), exp_ms, gain), shrink=1)
    if planet(img)[0] is None:
        log("ABORT: no planet in the window after the exposure test -- not recording empty sky")
        sys.exit(3)
    if (a.max_dim_exp is not None and exp_ms > a.max_dim_exp) or (a.max_dim_gain is not None and gain > a.max_dim_gain):
        log("ABORT: too dim -- needed %.0f ms at gain %d (limits %s ms / gain %s): cloud, not recording" % (exp_ms, gain, a.max_dim_exp, a.max_dim_gain))
        sys.exit(4)
    cx, cy = w / 2.0, h / 2.0

    # -- hold loop, with the recorder running --------------------------------
    def correct(q, J):
        need = np.array([cx - q[0], cy - q[1]])
        corr = np.linalg.solve(J, need)                     # arcmin RA, Dec
        global m
        try:
            st = m.state()
        except Exception:
            log("  mount socket dropped (idle) -> reconnecting"); m = Mount(host()); st = m.state()
        ra = st["RA"] + corr[0] / (60.0 * 15.0 * math.cos(math.radians(st["Dec"]))); dec = st["Dec"] + corr[1] / 60.0
        try:
            m.goto(ra, dec, wait=True, timeout=20)
        except Exception as e:
            log("  goto raised %s -> reconnecting and retrying once" % e.__class__.__name__); m = Mount(host()); m.goto(ra, dec, wait=True, timeout=20)
        return corr, float(np.hypot(*need))

    q, desc = planet(img)
    last_corr = 0.0; flipped = False; pending = None
    if q and not a.no_hold and math.hypot(cx - q[0], cy - q[1]) > a.deadband:
        corr, r0 = correct(q, J); last_corr = time.time(); pending = r0
        log("pre-record centring: RA %+.2f' Dec %+.2f' (was %.0f px off)" % (corr[0], corr[1], r0))
        fresh_frame(p); img, w, h, info = fresh_frame(p); q, desc = planet(img)
        if q:
            r1 = math.hypot(cx - q[0], cy - q[1])
            log("  now %.0f px off (%s)" % (r1, desc))
            if r1 > r0 * 1.2:
                J = -J; flipped = True; log("  moved the wrong way -> Jacobian negated")
                corr, r0 = correct(q, J); fresh_frame(p); img, w, h, info = fresh_frame(p); q, desc = planet(img)
                if q: log("  after re-correction: %.0f px off" % math.hypot(cx - q[0], cy - q[1]))
    p.s.air.drain_events()
    log("start_record_avi -> %s" % p.c("start_record_avi"))
    t0 = time.time(); offs = []; n = 0; lost = 0
    while time.time() - t0 < a.seconds:
        try:
            img, w, h, info = p.grab(timeout=5)
        except Exception as e:
            log("  grab failed: %s" % e); time.sleep(0.5); continue
        for e in p.s.air.drain_events():
            if e.get("Event") in ("AviRecord", "VideoCapture"):
                avi_events.append(e)
        if not info.get("fresh"):
            continue
        n += 1
        q, desc = planet(img)
        if q is None:
            lost += 1; log("  %5.1fs: no planet (%s)" % (time.time() - t0, desc)); continue
        off = math.hypot(cx - q[0], cy - q[1]); offs.append(off)
        ev = avi_events[-1] if avi_events else {}
        log("  %5.1fs: planet (%.0f,%.0f) off %.0f px  peak %.0f  | avi working=%s fps=%s write_fps=%s lapse=%s" % (
            time.time() - t0, q[0], q[1], off, q[2], ev.get("is_working"), ev.get("fps"), ev.get("write_file_fps"), ev.get("lapse_sec")))
        if not a.no_hold and off > a.deadband and time.time() - last_corr > a.min_gap:
            corr, r0 = correct(q, J); last_corr = time.time()
            log("  correction: RA %+.2f' Dec %+.2f'" % (corr[0], corr[1]))
    log("stop_record_avi -> %s" % p.c("stop_record_avi"))
    el = time.time() - t0
    for e in p.s.air.drain_events():
        if e.get("Event") in ("AviRecord", "VideoCapture"):
            avi_events.append(e)
    ev = avi_events[-1] if avi_events else {}
    log("RECORDED %.1fs ; last AviRecord: %s" % (el, {k: ev.get(k) for k in ("state", "is_working", "lapse_sec", "fps", "write_file_fps")}))
    if offs:
        log("hold: %d frames measured, %d without planet ; offset median %.0f px, p90 %.0f px, max %.0f px (%.1f\"/px)" % (
            n, lost, float(np.median(offs)), float(np.percentile(offs, 90)), max(offs), 0.294))
    save_png(img, "%s/%s_last.png" % (a.outdir, time.strftime("%H%M%S")), shrink=1)
    log("exposure used: %.1f ms gain %d, ROI %dx%d at (%d,%d), bin 1 ; the AVI is in Video/ on the Air's SMB share" % (exp_ms, gain, a.roi, a.roi, x0, y0))
finally:
    try: p.c("stop_record_avi")
    except Exception: pass
    p.close()
    try:
        try:
            st = m.state()
        except Exception:
            m = Mount(host()); st = m.state()
        log("end: register RA %.4f Dec %.4f track=%s" % (st["RA"], st["Dec"], st["is_enable_track"]))
    except Exception: pass
    m.close()
