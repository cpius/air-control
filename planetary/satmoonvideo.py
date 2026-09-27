#!/usr/bin/env python3
"""Self-stopping moon video: a short, fast recording for SHARP moons (lucky imaging), stopped as soon
as every required moon will stack to --target-snr.

Why (2026-09-27): the 1 s satmoons.py frames average the seeing and smear ~1.5" of drift, so the moons
came out ~2.5" wide; the 45 ms planet clips showed Mimas at 0.6-0.7". Scaled from Mimas in those clips,
100 ms frames give per-frame SNR ~90 Titan, ~28 Rhea, ~15 Tethys/Dione, ~4 Enceladus: 20-60 s is
enough for the four bright ones, ~2 min for Enceladus. So the length is decided live, not fixed.

  1. moonmeter.MoonMeter: Horizons offsets -> a flat readout window that holds Saturn and the wanted
     moons (bin 1, 8-bit: the video page forces both), Saturn's spot in it.
  2. rtmp page, set_subframe(window), fixed --exp-ms / --gain (Saturn burns out on purpose; it comes
     from the planet clips), Saturn nudged to its spot with the planet hold's pulses, confirmed on
     --confirm frames, start_record_avi.
  3. Every fresh preview frame: 2x2-binned luminance -> MoonMeter.update (moons measured and stacked
     on themselves), Saturn held at its spot, heartbeat with each moon's projected SNR.
  4. Stop when every --required moon is projected at --target-snr (after --min-seconds), or at
     --max-seconds, or when Saturn is lost / frames stall.

Exit: 0 target reached   7 max-seconds hit first (still a usable clip)   3 no Saturn before recording
      5 Saturn lost while recording   6 frames stalled   1 anything else
Outputs <outdir>/<HHMMSS>_moonvideo.csv (per sampled frame) and .json (window, spot, times, SNRs).

    ASIAIR_HOST=192.168.1.36 python3 -u satmoonvideo.py --east=0.9875,0.1578 --arcsec-per-px 0.09968
"""
import argparse, csv, json, math, os, signal, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daypipes import host, Pipes, log, save_png
from main_image import MainImage
from mount import Mount
from planetdetect import FreshFrames
from moonmeter import MoonMeter

ap = argparse.ArgumentParser()
ap.add_argument("--exp-ms", type=float, default=100.0, help="short enough to freeze most of the seeing and the drift (1.5\"/s -> 0.15\")")
ap.add_argument("--gain", type=int, default=450)
ap.add_argument("--moons", default="Titan,Rhea,Dione,Tethys,Enceladus,Mimas", help="measured and fitted into the window")
ap.add_argument("--required", default="Titan,Rhea,Dione,Tethys", help="the recording stops when all of these reach --target-snr (add Enceladus for ~2 min clips)")
ap.add_argument("--target-snr", type=float, default=40.0, help="projected SNR of each required moon's final stack")
ap.add_argument("--keep", type=float, default=0.25, help="fraction of frames the stacker will keep (projection only)")
ap.add_argument("--min-seconds", type=float, default=20.0)
ap.add_argument("--max-seconds", type=float, default=150.0)
ap.add_argument("--margin-arcsec", type=float, default=15.0, help="window margin around Saturn and the moons")
ap.add_argument("--min-height", type=int, default=400, help="window height floor, bin-1 px")
ap.add_argument("--write-mbps", type=float, default=12.0, help="the Air's AVI write rate, MB/s (1920x1080 at 5.7 fps, 2026-09); used when AviRecord gives no fps")
ap.add_argument("--east", required=True, help="sky east on the sensor (moontrails.py plate solve: 0.9875,0.1578 on 2026-09-27)")
ap.add_argument("--arcsec-per-px", type=float, default=0.09968, help="bin-1 scale")
ap.add_argument("--deadband", type=float, default=120.0, help="bin-1 px: no correction inside this")
ap.add_argument("--min-gap", type=float, default=6.0, help="seconds between corrections")
ap.add_argument("--hold-gain", type=float, default=0.6)
ap.add_argument("--confirm", type=int, default=2, help="fresh frames with Saturn at its spot before start_record_avi")
ap.add_argument("--max-missing", type=int, default=8, help="stop after this many consecutive fresh frames without Saturn")
ap.add_argument("--max-stall", type=float, default=30.0)
ap.add_argument("--heartbeat", type=float, default=5.0)
ap.add_argument("--outdir", default="/Users/madsdorup/ASICAP/telemetry/video")
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)
CHIP = (3840, 2160)
E = np.array([float(v) for v in a.east.split(",")]); E /= np.linalg.norm(E); N = np.array([-E[1], E[0]])
STAMP = time.strftime("%H%M%S")


def _terminate(signum, _frame):
    raise SystemExit(128 + signum)                  # run the finally: stop_record_avi, tracking on
signal.signal(signal.SIGTERM, _terminate)


def download(wait=20.0):
    for attempt in (1, 2):
        try:
            hdr, files = p.s.img.get_image("get_current_img", 0, wait=wait)
            return next(iter(files.values())), hdr["width"], hdr["height"], bool(hdr["isBigEndian"])
        except Exception as e:
            if attempt == 2: raise
            log("  4800 download failed (%s: %s) -> new image socket" % (e.__class__.__name__, e))
            try: p.s.img.close()
            except Exception: pass
            p.s.img = MainImage(host())


def lum2(img):                                      # raw RGGB, bin 1 -> luminance at 2x2-binned scale
    x = img.astype(np.float32); h, w = (x.shape[0] // 2) * 2, (x.shape[1] // 2) * 2; x = x[:h, :w]
    return x[0::2, 0::2] + x[0::2, 1::2] + x[1::2, 0::2] + x[1::2, 1::2]


def saturn_bin1(lum):
    S = MoonMeter.find_saturn(lum)
    return None if S is None else S * 2 + 0.5


def correct_pulse(q, target):
    """Saturn (bin-1 window px q) to its spot with pulses; returns (east", north") the pointing moved."""
    from air_rpc import Air
    dd = np.array(q) - np.array(target); e_as, n_as = float(dd @ E) * a.arcsec_per_px * a.hold_gain, float(dd @ N) * a.arcsec_per_px * a.hold_gain
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
    if abs(n_as) > 8: pulse("south" if n_as > 0 else "north", min(abs(n_as) / 312.0, 0.5))    # pier west: 'south' raises Dec
    if e_as > 8:
        try: mdo(lambda mm: mm.call("scope_set_track_state", [False])); time.sleep(min(e_as / 15.0, 6.0))
        finally: mdo(lambda mm: mm.call("scope_set_track_state", [True]))
    elif e_as < -8:
        pulse("west", min(-e_as / 312.0, 0.5))
    return e_as, n_as


m = Mount(host()); st = m.state(); st = m.state()
log("start: register RA %.4f Dec %.4f Alt %.1f Az %.1f pier=%s track=%s" % (st["RA"], st["Dec"], st["Alt"], st["Az"], st.get("pier_side"), st["is_enable_track"]))
if not st["is_enable_track"]:
    log("tracking was OFF -> %s" % m.set_tracking(True))
meter = MoonMeter(time.time(), E, a.arcsec_per_px * 2, a.moons.split(","), a.required.split(","), a.target_snr, a.keep, log=log)
win, spot, want = meter.plan_roi(time.time(), a.margin_arcsec, a.min_height, a.arcsec_per_px, CHIP)
log("plan: window %dx%d at (%d,%d) = %.0f\" x %.0f\" ; Saturn's spot (%.0f,%.0f) ; moons in it: %s ; required: %s" % (
    win["width"], win["height"], win["x"], win["y"], win["width"] * a.arcsec_per_px, win["height"] * a.arcsec_per_px, spot[0], spot[1],
    ", ".join(want), ", ".join(meter.required)))
fps_guess = min(1000.0 / a.exp_ms, a.write_mbps * 1e6 / (win["width"] * win["height"]))
log("expected ~%.1f recorded fps (%.2f MB/frame, %.0f ms exposures)" % (fps_guess, win["width"] * win["height"] / 1e6, a.exp_ms))

p = Pipes()
frames = FreshFrames(download, want=(win["width"], win["height"]), log=log)
recording = False; code = 1; csvf = None; avi_events = []; summary = {}
try:
    frames.remember("before setup")
    p.setup("rtmp", a.exp_ms / 1000.0, a.gain, 1)
    p.c("stop_exposure"); time.sleep(0.8)
    r = p.c("set_subframe", [win]); sf = p.c("get_subframe")
    if not (isinstance(sf, dict) and sf.get("width") == win["width"] and sf.get("height") == win["height"]):
        r = p.c("set_subframe", win); sf = p.c("get_subframe")
    log("subframe -> %s ; reads back %s" % (r, sf))
    if not (isinstance(sf, dict) and sf.get("width") == win["width"] and sf.get("height") == win["height"]):
        raise RuntimeError("window not applied: %s" % sf)
    p.c("set_control_value", ["Exposure", int(a.exp_ms * 1000)]); p.c("set_control_value", ["Gain", a.gain]); p.exp = a.exp_ms / 1000.0
    log("exposure %.1f ms gain %d ; bin %s" % (a.exp_ms, a.gain, p.c("get_camera_bin")))
    frames.remember("after stop_exposure")
    p.s.air.drain_events()
    log("start_exposure(light) -> %s ; waiting for the first fresh %dx%d frame" % (p.c("start_exposure", ["light"]), win["width"], win["height"]))
    f = frames.get(120, what="first frame")
    if f is None:
        raise RuntimeError("no new %dx%d frame in 120 s" % (win["width"], win["height"]))

    # -- Saturn to its spot, then confirm -------------------------------------------------------
    last_corr = 0.0
    for it in range(6):
        S = saturn_bin1(lum2(f.img))
        if S is None:
            log("setup frame #%d: Saturn not in the window" % f.n); f = frames.get(30, what="setup frame")
            if f is None: break
            continue
        off = float(np.hypot(*(S - np.array(spot))))
        log("setup frame #%d: Saturn at (%.0f,%.0f), %.0f px from its spot" % (f.n, S[0], S[1], off))
        if off <= a.deadband: break
        e, n = correct_pulse(S, spot); last_corr = time.time()
        log("  nudged: pointing %+.0f\" east %+.0f\" north" % (e, n))
        frames.skip(1, timeout=10); f = frames.get(30, what="frame after nudge")
        if f is None: break
    ok = 0
    for it in range(a.confirm + 4):
        f = frames.get(30, what="confirm frame")
        S = None if f is None else saturn_bin1(lum2(f.img))
        ok = ok + 1 if S is not None else 0
        if ok >= a.confirm: break
    if ok < a.confirm:
        log("ABORT: Saturn not confirmed in the window -- not recording"); code = 3; sys.exit(code)

    # -- record, measure, stop when the moons have enough ---------------------------------------
    p.s.air.drain_events()
    log("start_record_avi -> %s" % p.c("start_record_avi"))
    recording = True; t0 = time.time(); last_hb = t0; missing = 0; status = "max-seconds"
    csvf = open("%s/%s_moonvideo.csv" % (a.outdir, STAMP), "w", newline=""); rows = csv.writer(csvf)
    rows.writerow(["t_s", "frame", "saturn_x", "saturn_y"] + ["snr_" + mn for mn in want])
    while True:
        el = time.time() - t0
        if el >= a.max_seconds:
            status = "max-seconds"; break
        try:
            f = frames.get(5.0, heartbeat=1e9)
        except Exception as e:
            log("  %5.1fs: frame download failed: %s" % (el, e)); f = None; time.sleep(0.5)
        for ev in p.s.air.drain_events():
            if ev.get("Event") in ("AviRecord", "VideoCapture"): avi_events.append(ev)
        if f is None:
            if time.time() - frames.last_t > a.max_stall:
                status = "stalled"; log("STOPPING: no fresh frame for %.0f s" % (time.time() - frames.last_t)); break
            continue
        lum = lum2(f.img); S2 = meter.update(lum, time.time())
        if S2 is None:
            missing += 1; log("  %5.1fs: fresh frame #%d without Saturn (%d in a row)" % (el, f.n, missing))
            if missing >= a.max_missing:
                status = "lost"; log("STOPPING: Saturn gone for %d frames" % missing); break
            continue
        missing = 0; S = S2 * 2 + 0.5
        rows.writerow(["%.2f" % el, f.n, "%.1f" % S[0], "%.1f" % S[1]] + ["%.1f" % meter.last[mn]["snr"] if meter.last.get(mn) else "" for mn in want])
        wfps = next((ev.get("write_file_fps") for ev in reversed(avi_events) if ev.get("write_file_fps")), None)
        recorded = (wfps or fps_guess) * el
        line, done = meter.line(recorded)
        if time.time() - last_hb >= a.heartbeat:
            last_hb = time.time()
            log("  %5.1fs: %d sampled, ~%.0f recorded (%s fps) | %s" % (el, meter.frames, recorded, "%.1f" % wfps if wfps else "~%.1f est" % fps_guess, line))
        if done and el >= a.min_seconds:
            status = "done"; log("TARGET REACHED after %.1f s: %s" % (el, line)); break
        off = float(np.hypot(*(S - np.array(spot))))
        if off > a.deadband and time.time() - last_corr > a.min_gap:
            e, n = correct_pulse(S, spot); last_corr = time.time()
            log("  %5.1fs: hold: pointing %+.0f\" east %+.0f\" north (Saturn was %.0f px off its spot)" % (time.time() - t0, e, n, off))
            frames.skip(1, timeout=5)
    log("stop_record_avi -> %s" % p.c("stop_record_avi")); recording = False
    el = time.time() - t0
    for ev in p.s.air.drain_events():
        if ev.get("Event") in ("AviRecord", "VideoCapture"): avi_events.append(ev)
    wfps = next((ev.get("write_file_fps") for ev in reversed(avi_events) if ev.get("write_file_fps")), None)
    stat, done = meter.status((wfps or fps_guess) * el)
    summary = dict(stamp=STAMP, t_start=t0, t_end=t0 + el, seconds=el, status=status, window=win, saturn_spot=list(spot), exp_ms=a.exp_ms, gain=a.gain,
                   write_fps=wfps, sampled=meter.frames, moons={mn: dict(projected_snr=s, ok=bool(k)) for mn, (s, k) in stat.items()},
                   learned_offset_px_bin2={mn: (np.median(v, axis=0).tolist() if v else None) for mn, v in meter.learned.items()},
                   required=meter.required, left_out=sorted(meter.skip), east=E.tolist(), arcsec_per_px=a.arcsec_per_px)
    json.dump(summary, open("%s/%s_moonvideo.json" % (a.outdir, STAMP), "w"), indent=1)
    code = {"done": 0, "max-seconds": 7, "lost": 5, "stalled": 6}[status]
    log("RESULT %s after %.1f s: %s ; window %dx%d, %.0f ms g%d ; %s" % (status, el, meter.line((wfps or fps_guess) * el)[0], win["width"], win["height"],
        a.exp_ms, a.gain, "%s/%s_moonvideo.json" % (a.outdir, STAMP)))
    if f is not None: save_png(f.img, "%s/%s_moonvideo_last.png" % (a.outdir, STAMP), shrink=2)
except KeyboardInterrupt:
    log("interrupted"); code = 130
finally:
    signal.signal(signal.SIGTERM, signal.SIG_IGN); signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        if recording:
            try: log("stop_record_avi (cleanup) -> %s" % p.c("stop_record_avi"))
            except Exception as e: log("stop_record_avi in cleanup FAILED: %s -- the Air may still be recording" % e)
        else:
            try: p.c("stop_record_avi")
            except Exception: pass
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL); signal.signal(signal.SIGINT, signal.default_int_handler)
    if csvf: csvf.close()
    p.close()
    try:
        try: st = m.state()
        except Exception: m = Mount(host()); st = m.state()
        if not st["is_enable_track"]: log("tracking OFF at the end -> %s" % m.set_tracking(True))
        log("end: register RA %.4f Dec %.4f track=%s" % (st["RA"], st["Dec"], st["is_enable_track"]))
    except Exception: pass
    m.close()
sys.exit(code)
