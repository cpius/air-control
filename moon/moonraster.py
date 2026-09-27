#!/usr/bin/env python3
"""Find the Moon by sweeping rows of sky with the video stream running.

    ASIAIR_HOST=192.168.1.36 python3 -u moon/moonraster.py --ra 21.077 --dec -19.19 \
        --dec-lo -2 --dec-hi 4 --ra-lo -6 --ra-hi 3 --row-step 25

Why: a 31' disc is unmissable even badly defocused, whereas scattered moonlight
is nearly flat for degrees around it (2026-09-22: +-4% over a 1 deg cross), so
a gradient climb finds nothing. Rows are register Dec offsets from --dec (deg),
each swept in register RA from --ra-lo to --ra-hi (on-sky degrees, boustrophedon)
at slew-rate index --rate with the rtmp page streaming a small ROI at a few ms
exposure: sky is black, lunar surface saturates. Rows are visited centre-out.
On a bright frame the mount stops, settles, confirms with a fresh frame, and if
the stop overshot it creeps back at --creep-rate until the disc is back.
Every direction sign is measured with a short nudge, never assumed.
Guards: --min-alt, per-row and total wall-clock caps, stop in a finally.
"""
import argparse, math, os, signal, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from daypipes import host, Pipes, log, save_png
from main_image import MainImage
from planetdetect import FreshFrames
from joystick import Joystick

ap = argparse.ArgumentParser()
ap.add_argument("--ra", type=float, required=True, help="register RA (h) the Moon should be at")
ap.add_argument("--dec", type=float, required=True, help="register Dec (deg)")
ap.add_argument("--dec-lo", type=float, default=-2.0); ap.add_argument("--dec-hi", type=float, default=4.0)
ap.add_argument("--ra-lo", type=float, default=-6.0); ap.add_argument("--ra-hi", type=float, default=3.0)
ap.add_argument("--row-step", type=float, default=25.0, help="arcmin between rows (Moon is 31')")
ap.add_argument("--rate", type=int, default=5, help="slew-rate index for the sweep (5=60x, ~16'/s)")
ap.add_argument("--creep-rate", type=int, default=4)
ap.add_argument("--roi", type=int, default=640)
ap.add_argument("--exp-ms", type=float, default=5.0); ap.add_argument("--gain", type=int, default=200)
ap.add_argument("--bright", type=float, default=40.0, help="8-bit level that counts as lunar surface")
ap.add_argument("--bright-frac", type=float, default=0.02, help="fraction of pixels above --bright")
ap.add_argument("--min-alt", type=float, default=8.0)
ap.add_argument("--row-cap", type=float, default=90.0, help="seconds per row before giving up on it")
ap.add_argument("--total-cap", type=float, default=900.0)
ap.add_argument("--outdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "telemetry", time.strftime("%Y-%m-%d"), "moonraster"))
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)
CHIP = (3840, 2160)
T0 = time.time()
# SIGTERM must run the finally (stop the mount), same as Ctrl-C
signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))

j = Joystick(host())
p = Pipes()

def download(wait=20.0):
    for attempt in (1, 2):
        try:
            hdr, files = p.s.img.get_image("get_current_img", 0, wait=wait)
            return next(iter(files.values())), hdr["width"], hdr["height"], bool(hdr["isBigEndian"])
        except Exception as e:
            if attempt == 2:
                raise
            log("  4800 download failed (%s) -> new image socket" % e.__class__.__name__)
            try: p.s.img.close()
            except Exception: pass
            p.s.img = MainImage(host())

frames = FreshFrames(download, want=(a.roi, a.roi), log=log)

def st(): return j.state()

def err_arcmin(s, ra, dec):
    dra = ((ra - s["RA"] + 12) % 24 - 12) * 15 * math.cos(math.radians(dec)) * 60
    return dra, (dec - s["Dec"]) * 60

sign = {}
rate = {}

def learn(axis):
    names = ("east", "west") if axis == "ra" else ("north", "south")
    j.set_rate(a.rate); time.sleep(0.2)
    dra, ddec, b, c = j.nudge(names[0], 0.5)
    moved = dra if axis == "ra" else ddec
    sign[axis] = names[0] if moved > 0 else names[1]
    rate[axis] = max(abs(moved) / 0.5, 0.05)
    log("  %s: '%s' raises the register, %.3f deg/s at rate %d" % (axis, sign[axis], rate[axis], a.rate))

def dirn(axis, up):
    names = ("east", "west") if axis == "ra" else ("north", "south")
    return sign[axis] if up else (names[1] if sign[axis] == names[0] else names[0])

def move_to(ra, dec, tol=2.0, tries=10):
    j.set_rate(a.rate); time.sleep(0.2)
    for _ in range(tries):
        s = st()
        if s["Alt"] < a.min_alt:
            raise SystemExit("alt %.2f below %.1f -- stopped" % (s["Alt"], a.min_alt))
        e = dict(zip(("ra", "dec"), err_arcmin(s, ra, dec)))
        axis = max(e, key=lambda k: abs(e[k]))
        if abs(e[axis]) < tol:
            return s
        secs = max(0.25, min(3.0, abs(e[axis]) / 60 * 0.85 / rate[axis]))
        dra, ddec, b, c = j.nudge(dirn(axis, e[axis] > 0), secs)
    return st()

def is_bright(img):
    return float((img > a.bright).mean())

def snap(tag, f):
    fn = os.path.join(a.outdir, "%s_%s.png" % (time.strftime("%H%M%S"), tag))
    save_png(f.img.astype(np.float32), fn, shrink=1)
    return fn

def sweep_row(dec, ra_from, ra_to):
    """Sweep one row in register RA at a.rate with frames streaming. Returns a Frame on detection."""
    going_up = ra_to > ra_from
    j.set_rate(a.rate); time.sleep(0.2)
    j.move(dirn("ra", going_up))
    t0 = time.time(); n = 0; last_reg = None
    try:
        while time.time() - t0 < a.row_cap:
            f = frames.get(5.0, what="sweep frame")
            if f is not None:
                n += 1
                fr = is_bright(f.img)
                if fr >= a.bright_frac:
                    j.stop()
                    s = st()
                    log("BRIGHT frame #%d: %.1f%% of pixels > %.0f (max %d) at register RA %.4f Dec %+.3f Alt %.2f Az %.2f -> %s"
                        % (f.n, 100 * fr, a.bright, int(f.img.max()), s["RA"], s["Dec"], s["Alt"], s["Az"], snap("bright", f)))
                    return f
            s = st(); last_reg = s
            if s["Alt"] < a.min_alt:
                j.stop(); raise SystemExit("alt %.2f below %.1f during a row -- stopped" % (s["Alt"], a.min_alt))
            dra, _ = err_arcmin(s, ra_to, dec)
            if (dra <= 0) == going_up:          # passed the end of the row
                break
            if n and n % 20 == 0:
                log("  row Dec %+.2f: %2d frames, RA %.4f (%.0f' to go), alt %.1f, %.0fs" % (dec, n, s["RA"], abs(dra), s["Alt"], time.time() - t0))
    finally:
        j.stop()
    log("  row Dec %+.2f done: %d frames in %.0fs, ended at RA %.4f (%s)" % (dec, n, time.time() - t0, (last_reg or st())["RA"], "cap" if time.time() - t0 >= a.row_cap else "end"))
    return None

def reacquire(back_dir):
    """The stop may have carried past the disc: creep back until bright again."""
    time.sleep(1.0); frames.skip(1, timeout=10)
    f = frames.get(10, what="confirm frame")
    if f is not None and is_bright(f.img) >= a.bright_frac:
        log("confirmed on the disc: %.1f%% bright -> %s" % (100 * is_bright(f.img), snap("confirm", f)))
        return f
    log("overshot (confirm frame %s) -- creeping back '%s' at rate %d" % ("dark" if f is not None else "missing", back_dir, a.creep_rate))
    j.set_rate(a.creep_rate); time.sleep(0.2); j.move(back_dir); t0 = time.time()
    try:
        while time.time() - t0 < 60:
            f = frames.get(5.0, what="creep frame")
            if f is not None and is_bright(f.img) >= a.bright_frac:
                j.stop(); time.sleep(0.8)
                log("back on the disc after %.0fs -> %s" % (time.time() - t0, snap("reacq", f)))
                return f
            s = st()
            if s["Alt"] < a.min_alt:
                break
    finally:
        j.stop()
    log("could not re-find the disc while creeping back")
    return None

try:
    log("ephemeris register target RA %.4f Dec %+.3f ; rows Dec %+.1f..%+.1f step %.0f' ; RA %+.1f..%+.1f deg on sky"
        % (a.ra, a.dec, a.dec_lo, a.dec_hi, a.row_step, a.ra_lo, a.ra_hi))
    # -- video stream: rtmp page, small ROI, short exposure ------------------
    frames.remember("before setup")
    p.setup("rtmp", a.exp_ms / 1000.0, a.gain, 1)
    p.c("stop_exposure"); time.sleep(0.8)
    x0, y0 = (CHIP[0] - a.roi) // 2, (CHIP[1] - a.roi) // 2
    want = {"x": x0, "y": y0, "width": a.roi, "height": a.roi}
    r = p.c("set_subframe", [want]); sf = p.c("get_subframe")
    if not (isinstance(sf, dict) and sf.get("width") == a.roi):
        r = p.c("set_subframe", want); sf = p.c("get_subframe")
    log("subframe -> %s ; reads back %s" % (r, sf))
    if not (isinstance(sf, dict) and sf.get("width") == a.roi):
        raise RuntimeError("ROI not applied: %s" % sf)
    p.c("set_control_value", ["Exposure", int(a.exp_ms * 1000)]); p.c("set_control_value", ["Gain", a.gain])
    frames.remember("after stop_exposure")
    p.s.air.drain_events()
    log("start_exposure(light) -> %s ; waiting for the first fresh %dx%d frame (30-70 s)" % (p.c("start_exposure", ["light"]), a.roi, a.roi))
    f = frames.get(120, what="first frame")
    if f is None:
        raise RuntimeError("no video frame in 120 s")
    log("first frame after %.0fs: %dx%d %d-bit, bright frac %.3f, max %d" % (time.time() - T0, f.w, f.h, f.depth, is_bright(f.img), int(f.img.max())))
    t = time.time(); k = 0
    while time.time() - t < 3:
        g = frames.get(1.0, what="rate test")
        if g is not None: k += 1
    log("stream rate ~%.1f frames/s" % (k / 3.0))

    # -- signs, then rows centre-out ---------------------------------------
    s = st(); log("register now RA %.4f Dec %+.3f Alt %.2f Az %.2f pier %s" % (s["RA"], s["Dec"], s["Alt"], s["Az"], s["pier_side"]))
    learn("dec"); learn("ra")
    n_rows = int(round((a.dec_hi - a.dec_lo) * 60 / a.row_step)) + 1
    offs = [a.dec_lo + i * a.row_step / 60 for i in range(n_rows)]
    offs.sort(key=lambda d: (abs(d), -d))          # centre-out, upper row first on ties
    found = None
    cosd = math.cos(math.radians(a.dec))
    for i, doff in enumerate(offs):
        if time.time() - T0 > a.total_cap:
            log("total cap %.0fs reached" % a.total_cap); break
        dec = a.dec + doff
        lo, hi = a.ra + a.ra_lo / 15 / cosd, a.ra + a.ra_hi / 15 / cosd
        ra_from, ra_to = (lo, hi) if i % 2 == 0 else (hi, lo)
        s = move_to(ra_from, dec)
        log("row %d/%d: Dec %+.3f (%+.0f'), from RA %.4f to %.4f ; alt %.1f az %.1f ; %.0fs elapsed"
            % (i + 1, n_rows, dec, doff * 60, ra_from, ra_to, s["Alt"], s["Az"], time.time() - T0))
        f = sweep_row(dec, ra_from, ra_to)
        if f is not None:
            found = reacquire(dirn("ra", not (ra_to > ra_from)))
            if found is not None:
                break
    if found is None:
        log("NOT FOUND in %d rows, %.0fs" % (len(offs), time.time() - T0)); sys.exit(2)
    s = st()
    dra, ddec = err_arcmin(s, a.ra, a.dec)
    log("MOON FOUND at register RA %.4f Dec %+.3f (alt %.2f az %.2f); ephemeris minus register: %+.0f' RA, %+.0f' Dec (on sky; somewhere on a 31' disc)"
        % (s["RA"], s["Dec"], s["Alt"], s["Az"], dra, ddec))
finally:
    try: j.stop()
    finally: j.close()
