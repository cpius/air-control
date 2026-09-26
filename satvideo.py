#!/usr/bin/env python3
"""Record a planetary AVI with the Air's own recorder while HOLDING the planet
centred with small gotos -- and only while the planet is provably in the frames.

Why gotos and not joystick pulses: a `scope_move` needs its `["none"]` stop to
arrive, and on the east balcony at -68 dBm that packet has been lost before
(see wifi-band-is-a-safety-issue). A goto of a few arcseconds stops by itself
and tonight completes in ~1 s. The frames smeared during a move are a handful
out of a thousand; the stacker's quality ranking drops them.

Sequence (order matters, measured 2026-09-02/10):
  preview page: bin 1 + full subframe   (set_subframe fails "out of limit" at bin 2)
  set_page(["rtmp"])                    (the video tab; resets the subframe, auto-starts)
  stop_exposure -> set_subframe ROI -> get_subframe to VERIFY -> exposure/gain
  start_exposure(["light"])             (first frame can take 30-70 s, then ~1-3 Hz on 4800)
  exposure test on the 8-bit frames     (planet peak into --peak-lo..--peak-hi of 255)
  confirm on --confirm-frames consecutive fresh frames
  start_record_avi ... hold loop ... stop_record_avi

What counts as "the planet is there" since 2026-09-17, when six 300 s clips were
logged as held ("hold: 1169 frames measured, 9 without planet") and were all
empty sky:
  * planetdetect.detect(): a blob far above THIS frame's measured noise and at
    least a quarter of the globe's disc in area. The old "peak > 10 counts over
    the sky" passed on 98% of the empty frames at gain 350-450.
  * A frame counts only if FreshFrames has never seen its bytes -- including the
    image the Air held before this run started -- and it is the ROI's size. The
    00:32:22 exposure test passed on such a cached frame (Saturn where the
    crashed run before had last seen it) while the recording was empty from
    frame 0. One fresh frame is discarded after every settings change and
    every correction.
  * No start_record_avi without --confirm-frames consecutive fresh frames with
    the planet. The recording is stopped after --max-missing consecutive fresh
    frames without it, or --max-stall seconds without any fresh frame.

Every fresh frame of the recording goes to <outdir>/<HHMMSS>_hold.csv.

Exit status (satloop.sh acts on it; only 0 prints RECORDED):
  0 recorded, planet held throughout    3 no planet before recording
  4 too dim (cloud), not recorded        5 recording stopped: planet lost
  6 recording stopped: frames stalled    1 anything else (traceback)

    ASIAIR_HOST=192.168.1.35 python3 -u satvideo.py --seconds 30 --roi 640 --exp-ms 18 --gain 250
"""
import argparse, csv, math, os, signal, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daypipes import host, Pipes, log, save_png
from main_image import MainImage
from mount import Mount
from planetdetect import detect, detector_args, detector_kw, min_area_px, FreshFrames, confirm, PlanetWatch

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
ap.add_argument("--hold-mode", default="goto", choices=["goto", "pulse"], help="pulse: no goto (register not trustworthy, 2026-09-26) -- Dec by 20x joystick pulses, RA east by pausing tracking, RA west by a pulse")
ap.add_argument("--east", default="0.239,0.971", help="--hold-mode pulse: sky east on the sensor (camangle.py)")
ap.add_argument("--exp-max", type=float, default=100.0, help="ms; the exposure test will not go longer than this")
ap.add_argument("--gain-max", type=int, default=450, help="raise the gain in steps of 50 up to this when the exposure cap is not enough")
ap.add_argument("--max-dim-exp", type=float, default=None, help="ms; if the exposure test needs more than this, treat it as cloud and do not record")
ap.add_argument("--max-dim-gain", type=int, default=None, help="if the exposure test needs more gain than this, treat it as cloud and do not record")
detector_args(ap)
ap.add_argument("--confirm-frames", type=int, default=2, help="consecutive fresh frames with the planet required before start_record_avi")
ap.add_argument("--confirm-radius", type=float, default=150.0, help="px: the planet may not jump further than this between confirmation frames")
ap.add_argument("--max-missing", type=int, default=10, help="stop the recording after this many consecutive fresh frames without the planet (~4 s at 2.5 Hz)")
ap.add_argument("--max-stall", type=float, default=30.0, help="stop the recording if no fresh frame arrives for this many seconds")
ap.add_argument("--heartbeat", type=float, default=5.0, help="seconds between hold-loop status lines")
ap.add_argument("--outdir", default="/Users/madsdorup/ASICAP/telemetry/video")
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)
J = np.array([float(v) for v in a.jacobian.split(",")]).reshape(2, 2)
CHIP = (3840, 2160)
KW = detector_kw(a)


def _terminate(signum, _frame):
    # satloop.sh stops a child with SIGTERM, by PID. Python's default would die on
    # the spot and leave the Air recording; SystemExit runs the finally below.
    raise SystemExit(128 + signum)


signal.signal(signal.SIGTERM, _terminate)


def find(f):
    return detect(f.img, depth=f.depth, **KW)


def download(wait=20.0):
    """Whatever image the Air holds now: (raw bytes, width, height, big_endian).
    A failed download gets one new image socket; a second failure raises."""
    for attempt in (1, 2):
        try:
            hdr, files = p.s.img.get_image("get_current_img", 0, wait=wait)
            return next(iter(files.values())), hdr["width"], hdr["height"], bool(hdr["isBigEndian"])
        except Exception as e:
            if attempt == 2:
                raise
            log("  4800 download failed (%s: %s) -> new image socket" % (e.__class__.__name__, e))
            try:
                p.s.img.close()
            except Exception:
                pass
            p.s.img = MainImage(host())


m = Mount(host()); st = m.state(); st = m.state()
log("start: register RA %.4f Dec %.4f Alt %.1f Az %.1f pier=%s track=%s" % (st["RA"], st["Dec"], st["Alt"], st["Az"], st.get("pier_side"), st["is_enable_track"]))
if not st["is_enable_track"]:
    log("tracking was OFF -> %s" % m.set_tracking(True))
p = Pipes()
frames = FreshFrames(download, want=(a.roi, a.roi), log=log)
log("detector: blob > %.0f sigma, peak > %.0f sigma and > %.1f counts, area >= %d px (%.0f%% of a %.1f\" globe at %.3f\"/px)" % (
    a.k_sigma, a.min_snr, a.min_amp, min_area_px(a.arcsec_per_px, a.planet_diam_arcsec, a.min_area_frac), 100 * a.min_area_frac, a.planet_diam_arcsec, a.arcsec_per_px))
avi_events = []
recording = False
code = 1
csvf = None
try:
    frames.remember("before setup")                  # the previous run's last image
    p.setup("rtmp", a.exp_ms / 1000.0, a.gain, 1)    # bin 1 + full subframe on preview, then the video page
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
    frames.remember("after stop_exposure")           # whatever the setup's own capture left behind
    p.s.air.drain_events()
    log("start_exposure(light) -> %s ; waiting for the first fresh %dx%d frame (can take 30-70 s)" % (p.c("start_exposure", ["light"]), a.roi, a.roi))
    t0 = time.time()
    f = frames.get(120, what="first frame")
    if f is None:
        raise RuntimeError("the rtmp page delivered no new %dx%d frame in 120 s" % (a.roi, a.roi))
    log("first fresh frame after %.0fs: %dx%d %d-bit, hash %s" % (time.time() - t0, f.w, f.h, f.depth, f.key[:8]))

    def next_frame(what, discard=0):
        """The next fresh frame, after discarding `discard` that may predate a change or a move."""
        if discard and not frames.skip(discard, timeout=30):
            raise RuntimeError("no new frame within 30 s while discarding (%s)" % what)
        g = frames.get(30, what=what)
        if g is None:
            raise RuntimeError("no new frame within 30 s (%s)" % what)
        return g

    # -- exposure test: put the planet's peak in the window ----------------
    for it in range(8):
        d = find(f)
        if not d.ok:
            log("exposure test %d: %.1f ms g%d, fresh frame #%d: %s -- taking another frame" % (it, exp_ms, gain, f.n, d))
            f = next_frame("exposure-test frame"); continue
        log("exposure test %d: %.1f ms g%d, fresh frame #%d -> %s" % (it, exp_ms, gain, f.n, d))
        pk = d.peak
        if a.peak_lo <= pk <= a.peak_hi:
            break
        new = exp_ms * (min(2.5, 1.15 * a.peak_lo / max(pk, 1.0)) if pk < a.peak_lo else 0.9 * a.peak_hi / pk)
        new = max(2.0, min(a.exp_max, new))
        if abs(new - exp_ms) < 0.3:
            if pk < a.peak_lo and gain + 50 <= a.gain_max:
                gain += 50; p.c("set_control_value", ["Gain", gain]); log("  exposure capped at %.0f ms -> gain %d" % (exp_ms, gain))
                f = next_frame("exposure-test frame", discard=1); continue
            break
        exp_ms = new
        p.c("set_control_value", ["Exposure", int(exp_ms * 1000)]); p.exp = exp_ms / 1000.0
        log("  exposure -> %.1f ms" % exp_ms)
        f = next_frame("exposure-test frame", discard=1)
    save_png(f.img, "%s/%s_test_%.0fms_g%d.png" % (a.outdir, time.strftime("%H%M%S"), exp_ms, gain), shrink=1)
    d = find(f)
    if not d.ok:
        log("ABORT: no planet in the window after the exposure test (%s) -- not recording empty sky" % d)
        code = 3
        sys.exit(code)
    if (a.max_dim_exp is not None and exp_ms > a.max_dim_exp) or (a.max_dim_gain is not None and gain > a.max_dim_gain):
        log("ABORT: too dim -- needed %.0f ms at gain %d (limits %s ms / gain %s): cloud, not recording" % (exp_ms, gain, a.max_dim_exp, a.max_dim_gain))
        code = 4
        sys.exit(code)
    cx, cy = f.w / 2.0, f.h / 2.0

    # -- hold loop, with the recorder running --------------------------------
    def correct_pulse(q):
        """Planet back to the ROI centre with pulses; returns ((east', north') the pointing moved, px off)."""
        from air_rpc import Air
        E = np.array([float(v) for v in a.east.split(",")]); E /= np.linalg.norm(E); N = np.array([-E[1], E[0]])
        dd = np.array([q.x - cx, q.y - cy]); e_as, n_as = float(dd @ E) * a.arcsec_per_px, float(dd @ N) * a.arcsec_per_px
        def mdo(fn):
            mm = Air(host(), 4400)
            try: return fn(mm)
            finally: mm.close()
        def pulse(cmd, secs):
            def f(mm):
                idx = mm.call("scope_get_info", [])["result"]["slew_rate_index"]
                try: mm.call("scope_set_slew_rate", [4]); mm.call("scope_move", [cmd]); time.sleep(secs)
                finally:
                    mm.call("scope_move", ["none"]); mm.call("scope_move", ["none"]); mm.call("scope_set_slew_rate", [idx])
            mdo(f)
        if abs(n_as) > 8:
            pulse("south" if n_as > 0 else "north", min(abs(n_as) / 312.0, 0.5))      # pier west: 'south' raises Dec
        if e_as > 8:
            try: mdo(lambda mm: mm.call("scope_set_track_state", [False])); time.sleep(min(e_as / 15.0, 6.0))
            finally: mdo(lambda mm: mm.call("scope_set_track_state", [True]))
        elif e_as < -8:
            pulse("west", min(-e_as / 312.0, 0.5))
        return np.array([e_as / 60.0, n_as / 60.0]), float(np.hypot(*dd))

    def correct(q, J):
        if a.hold_mode == "pulse":
            return correct_pulse(q)
        need = np.array([cx - q.x, cy - q.y])
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

    last_corr = 0.0; flipped = False
    if not a.no_hold and math.hypot(cx - d.x, cy - d.y) > a.deadband:
        corr, r0 = correct(d, J); last_corr = time.time()
        log("pre-record centring: RA %+.2f' Dec %+.2f' (was %.0f px off)" % (corr[0], corr[1], r0))
        f = next_frame("frame after centring", discard=1); d = find(f)
        if d.ok:
            r1 = math.hypot(cx - d.x, cy - d.y)
            log("  now %.0f px off (%s)" % (r1, d))
            if r1 > r0 * 1.2:
                J = -J; flipped = True; log("  moved the wrong way -> Jacobian negated")
                corr, r0 = correct(d, J); f = next_frame("frame after re-correction", discard=1); d = find(f)
                if d.ok: log("  after re-correction: %.0f px off" % math.hypot(cx - d.x, cy - d.y))
        else:
            log("  after centring: %s" % d)

    ok, dets, why = confirm(frames, find, need=a.confirm_frames, radius=a.confirm_radius,
                            tries=a.confirm_frames + 4, timeout=30, log=log)
    if not ok:
        log("ABORT: no planet in the window -- %s -- not recording empty sky" % why)
        code = 3
        sys.exit(code)
    p.s.air.drain_events()
    log("start_record_avi -> %s (planet confirmed on %d consecutive fresh frames)" % (p.c("start_record_avi"), len(dets)))
    recording = True
    t0 = time.time()
    csv_path = "%s/%s_hold.csv" % (a.outdir, time.strftime("%H%M%S"))
    csvf = open(csv_path, "w", newline="")
    rows = csv.writer(csvf)
    rows.writerow(["t_s", "frame", "hash", "planet", "x", "y", "off_px", "peak", "snr", "area_px", "why"])
    watch = PlanetWatch(a.max_missing)
    offs = []; status = "recorded"; last_hb = t0
    stale0, foreign0 = frames.stale, frames.foreign
    while time.time() - t0 < a.seconds:
        try:
            f = frames.get(5.0, heartbeat=1e9)
        except Exception as e:
            log("  %5.1fs: frame download failed: %s: %s" % (time.time() - t0, e.__class__.__name__, e)); f = None; time.sleep(0.5)
        for e in p.s.air.drain_events():
            if e.get("Event") in ("AviRecord", "VideoCapture"):
                avi_events.append(e)
        now = time.time()
        if f is None:
            gap = now - frames.last_t
            log("  %5.1fs: no fresh frame for %.0f s (%d stale re-reads so far)" % (now - t0, gap, frames.stale - stale0))
            if gap > a.max_stall:
                status = "stalled"
                log("STOPPING: no fresh frame for %.0f s -- cannot see what is being recorded" % gap)
                break
            continue
        d = find(f)
        stop = watch.update(d)
        off = math.hypot(cx - d.x, cy - d.y) if d.ok else None
        rows.writerow(["%.2f" % (now - t0), f.n, f.key[:16], int(d.ok),
                       "%.1f" % d.x if d.x is not None else "", "%.1f" % d.y if d.y is not None else "",
                       "%.1f" % off if off is not None else "", "%.0f" % d.peak, "%.1f" % d.snr, d.area, d.why])
        if not d.ok:
            log("  %5.1fs: fresh frame #%d: %s (%d in a row)" % (now - t0, f.n, d, watch.run))
            if stop:
                status = "lost"
                log("STOPPING: no planet in %d consecutive fresh frames" % watch.run)
                break
            continue
        offs.append(off)
        if now - last_hb >= a.heartbeat:
            last_hb = now
            ev = avi_events[-1] if avi_events else {}
            log("  %5.1fs: %d fresh frames, planet in %d ; now %s, %.0f px off | avi working=%s fps=%s write_fps=%s" % (
                now - t0, watch.frames, watch.with_planet, d, off, ev.get("is_working"), ev.get("fps"), ev.get("write_file_fps")))
        if not a.no_hold and off > a.deadband and now - last_corr > a.min_gap:
            corr, r0 = correct(d, J); last_corr = time.time()
            log("  %5.1fs: correction RA %+.2f' Dec %+.2f' (was %.0f px off)" % (time.time() - t0, corr[0], corr[1], r0))
            frames.skip(1, timeout=5)                   # exposed during the move: not judged
    log("stop_record_avi -> %s" % p.c("stop_record_avi"))
    recording = False
    el = time.time() - t0
    for e in p.s.air.drain_events():
        if e.get("Event") in ("AviRecord", "VideoCapture"):
            avi_events.append(e)
    ev = avi_events[-1] if avi_events else {}
    seen = "planet in %d of %d fresh frames (longest run without: %d), %d stale re-reads, %d foreign frames ignored ; per frame: %s" % (
        watch.with_planet, watch.frames, watch.longest, frames.stale - stale0, frames.foreign - foreign0, csv_path)
    if status == "recorded" and watch.with_planet == 0:  # never report an unseen clip as recorded
        status = "stalled" if watch.frames == 0 else "lost"
    if status == "recorded":
        log("RECORDED %.1fs ; %s ; last AviRecord: %s" % (el, seen, {k: ev.get(k) for k in ("state", "is_working", "lapse_sec", "fps", "write_file_fps")}))
        log("hold: %d frames measured, %d without planet ; offset median %.0f px, p90 %.0f px, max %.0f px (%.3f\"/px)%s" % (
            watch.frames, watch.frames - watch.with_planet, float(np.median(offs)), float(np.percentile(offs, 90)), max(offs),
            a.arcsec_per_px, " ; Jacobian was negated" if flipped else ""))
        code = 0
    elif status == "lost":
        log("STOPPED EARLY after %.1fs: planet lost ; %s -- the partial AVI on the Air is NOT a good clip" % (el, seen))
        code = 5
    else:
        log("STOPPED EARLY after %.1fs: frames stalled ; %s -- the AVI on the Air is unverified" % (el, seen))
        code = 6
    if f is not None:
        save_png(f.img, "%s/%s_last.png" % (a.outdir, time.strftime("%H%M%S")), shrink=1)
    log("exposure used: %.1f ms gain %d, ROI %dx%d at (%d,%d), bin 1 ; the AVI is in Video/ on the Air's SMB share" % (exp_ms, gain, a.roi, a.roi, x0, y0))
    log("RESULT %s" % status)
except KeyboardInterrupt:
    log("interrupted")
    code = 130
finally:
    # A second TERM or Ctrl-C must not cut stop_record_avi short -- but only that
    # call is shielded, so a hung close afterwards can still be interrupted.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        if recording:
            try:
                log("stop_record_avi (cleanup) -> %s" % p.c("stop_record_avi"))
            except Exception as e:
                log("stop_record_avi in cleanup FAILED: %s -- the Air may still be recording" % e)
        else:
            try: p.c("stop_record_avi")
            except Exception: pass
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.default_int_handler)
    if csvf:
        csvf.close()
    p.close()
    try:
        try:
            st = m.state()
        except Exception:
            m = Mount(host()); st = m.state()
        log("end: register RA %.4f Dec %.4f track=%s" % (st["RA"], st["Dec"], st["is_enable_track"]))
    except Exception: pass
    m.close()
sys.exit(code)
