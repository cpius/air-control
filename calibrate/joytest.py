#!/usr/bin/env python3
"""What does a joystick command REALLY move? Measured on lunar surface, not assumed.

2026-09-27: the planet hold's 20x pulses (300"/s) under- or over-shot by the Wi-Fi round trip
(a 9" correction moved 60" with a busy link, nothing with a quiet one). Two candidate fixes, tested here:
  * a SLOW slew rate (1x = 15"/s, 4x = 60"/s): the same RTT jitter is then 3-12" instead of 60";
  * the MOUNT-TIMED form scope_move [dir, sec] (from the app: MountUtils.keepMountMoving(direct, sec),
    the joystick sends [dir, 3] repeatedly while held) -- the mount stops by itself after `sec` whole
    seconds, so a lost "none" can never leave it running.

Each test: one focus-page frame before, the command, one frame discarded, two frames after; the shift of
the band-passed surface (moonreg) is converted to arcsec east/north with --east and --arcsec-per-px and
logged next to the register's own change. A 2 s tracking pause first calibrates sign and scale
(the pointing moves east at ~14.5"/s under Lunar tracking). Every direction is sent twice in a row so
the first move after a reversal (backlash) is visible on its own.

    ASIAIR_HOST=192.168.1.36 python3 -u joytest.py --east=-0.070,-0.998 --arcsec-per-px 0.1866
"""
import argparse, csv, math, os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from air_rpc import Air
from daypipes import Pipes, host, log
from moonreg import bandpass, measure_shift

ap = argparse.ArgumentParser()
ap.add_argument("--east", required=True, help="sky east on the sensor 'x,y' (camangle / driftscale)")
ap.add_argument("--arcsec-per-px", type=float, required=True, help="bin-1 scale")
ap.add_argument("--exp-ms", type=float, default=10.0); ap.add_argument("--gain", type=int, default=220)
ap.add_argument("--host-rates", default="0:0.25,0:1.0,2:0.25,2:1.0,4:0.1", help="slew-rate index:seconds pairs for host-timed pulses")
ap.add_argument("--timed-rates", default="0,2", help="slew-rate indices for the mount-timed [dir, 1] form")
ap.add_argument("--dirs", default="north,south,east,west")
ap.add_argument("--settle", type=float, default=0.8, help="s after a move before the discarded frame")
ap.add_argument("--outdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "telemetry", time.strftime("%Y-%m-%d"), "joytest"))
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)
E = np.array([float(v) for v in a.east.split(",")]); E /= np.linalg.norm(E); N = np.array([-E[1], E[0]])
res = lambda r: r.get("result", r) if isinstance(r, dict) else r

def mcall(fn):
    m = Air(host(), 4400, timeout=10)
    try: return fn(m)
    finally: m.close()
def reg():
    return mcall(lambda m: res(m.call("scope_get_info", [])))

p = Pipes()
def frame():
    img, w, h, _ = p.grab(timeout=30); img = img.astype(np.float64)
    g = img[0::2, 0::2] + img[1::2, 1::2] + img[0::2, 1::2] + img[1::2, 0::2]   # 2x2 superpixel: no Bayer
    return bandpass(g, 1.5, 20)
def shift_as(ref, mov):
    dx, dy, q = measure_shift(ref, mov, prepped=True)
    d = np.array([dx, dy]) * 2.0                                                  # superpixel -> sensor px
    return float(d @ E) * a.arcsec_per_px, float(d @ N) * a.arcsec_per_px, q
def reg_delta(s0, s1):
    return ((s1["RA"] - s0["RA"] + 12) % 24 - 12) * 54000.0 * math.cos(math.radians(s0["Dec"])), (s1["Dec"] - s0["Dec"]) * 3600.0

rows = []; sign = 1.0
csvf = open(os.path.join(a.outdir, time.strftime("%H%M%S") + "_joytest.csv"), "w", newline=""); W = csv.writer(csvf)
W.writerow(["test", "dir", "rate_idx", "secs", "method", "img_east_as", "img_north_as", "quality", "reg_east_as", "reg_north_as", "still_moving_as"])

def measure(label, act, d, rate, secs, method, check_stop=False):
    ref = frame(); s0 = reg()
    act()
    time.sleep(a.settle); frame()                                                 # exposed during/just after the move
    f1 = frame()
    e1, n1, q1 = shift_as(ref, f1)
    moving = ""
    if check_stop:                                                                # mount-timed: is it still going?
        time.sleep(1.5); f2 = frame(); e2, n2, _ = shift_as(ref, f2)
        moving = "%.1f" % math.hypot(e2 - e1, n2 - n1); e1, n1 = e2, n2
    s1 = reg(); re_, rn = reg_delta(s0, s1)
    # the pointing moves OPPOSITE to the image features; report the POINTING motion
    pe, pn = -sign * e1, -sign * n1
    W.writerow([label, d, rate, secs, method, "%.1f" % pe, "%.1f" % pn, "%.3f" % q1, "%.1f" % re_, "%.1f" % rn, moving]); csvf.flush()
    log("%-8s %-5s rate %d %5.2fs %-6s pointing moved %+6.1f\" E %+6.1f\" N  (register %+6.1f\" E %+6.1f\" N)%s  q %.2f" % (
        label, d, rate, secs, method, pe, pn, re_, rn, ("  still moving %s\" after 1.5 s" % moving) if moving else "", q1))
    rows.append((d, rate, secs, method, pe, pn))

def host_pulse(d, rate, secs):
    def f(m):
        idx = res(m.call("scope_get_info", []))["slew_rate_index"]
        try: m.call("scope_set_slew_rate", [rate]); m.call("scope_move", [d]); time.sleep(secs)
        finally: m.call("scope_move", ["none"]); m.call("scope_set_slew_rate", [idx])
    return lambda: mcall(f)
def timed_move(d, rate):
    def f(m):
        idx = res(m.call("scope_get_info", []))["slew_rate_index"]
        m.call("scope_set_slew_rate", [rate]); r = m.call("scope_move", [d, 1])
        log("   scope_move [%s, 1] -> %s" % (d, r.get("code", r) if isinstance(r, dict) else r))
        time.sleep(1.4); m.call("scope_set_slew_rate", [idx])                    # restore only after the timer ran out
    return lambda: mcall(f)

try:
    p.setup("focus", a.exp_ms / 1000.0, a.gain, 1); frame()
    s = reg(); log("start: RA %.5f Dec %.4f alt %.1f  track %s mode %s  slew index %d" % (s["RA"], s["Dec"], s["Alt"], s["is_enable_track"], s["track_mode_list"][s["track_mode_index"]], s["slew_rate_index"]))
    # 0. still frames: seeing + drift floor
    measure("null", lambda: None, "-", -1, 0, "none")
    # 1. calibration: tracking paused 2 s -> pointing moves EAST ~29"
    def pause():
        try: mcall(lambda m: m.call("scope_set_track_state", [False])); time.sleep(2.0)
        finally: mcall(lambda m: m.call("scope_set_track_state", [True]))
    ref = frame(); pause(); time.sleep(a.settle); frame(); e, n, q = shift_as(ref, frame())
    sign = -1.0 if e > 0 else 1.0                                                 # features must move WEST when the pointing goes east
    log("calibration: 2 s tracking pause moved the image %+.1f\" E %+.1f\" N (q %.2f) -> pointing %+.1f\" E (expect ~+29)" % (e, n, q, -sign * e))
    W.writerow(["calib", "pause", -1, 2.0, "trackoff", "%.1f" % (-sign * e), "%.1f" % (-sign * n), "%.3f" % q, "", "", ""])
    dirs = a.dirs.split(",")
    for spec in a.host_rates.split(","):
        rate, secs = int(spec.split(":")[0]), float(spec.split(":")[1])
        for d in dirs:
            if rate >= 4 and d in ("east", "west"): continue                     # 20x E-W would leave the 2.6' axis of the crop
            for rep in (1, 2):
                measure("host#%d" % rep, host_pulse(d, rate, secs), d, rate, secs, "host")
    for rate in (int(v) for v in a.timed_rates.split(",") if v != ""):
        for d in dirs:
            for rep in (1, 2):
                measure("timed#%d" % rep, timed_move(d, rate), d, rate, 1.0, "mount", check_stop=True)
finally:
    try: mcall(lambda m: (m.call("scope_move", ["none"]), m.call("scope_set_track_state", [True])))
    except Exception as e: log("!! final stop/track failed: %s -- CHECK THE MOUNT" % e)
    s = reg(); log("end: RA %.5f Dec %.4f track %s mode %s slew index %d" % (s["RA"], s["Dec"], s["is_enable_track"], s["track_mode_list"][s["track_mode_index"]], s["slew_rate_index"]))
    p.close(); csvf.close()

print("\nSUMMARY (second move of each pair = no reversal; expected = rate x seconds, 1x = 15\"/s)")
exp_rate = {0: 15.0, 1: 30.0, 2: 60.0, 3: 120.0, 4: 300.0}
for d, rate, secs, method, pe, pn in rows:
    if rate < 0: continue
    got = pn if d in ("north", "south") else pe
    print("  %-5s rate %d %-6s %4.2fs  expected %6.1f\"  got %+6.1f\"" % (d, rate, method, secs, exp_rate.get(rate, float("nan")) * secs, got))
