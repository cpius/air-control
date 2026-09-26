#!/usr/bin/env python3
"""Keep a bright planet centred, continuously, with pulses only (no goto) -- for breaks between
recordings: downloads, ADC trims. Preview page, bin 2, short exposure; every frame the planet's
centroid; beyond --tol arcsec: Dec by a 20x pulse (312"/s), RA east by pausing tracking (15"/s),
RA west by a pulse. Runs --seconds, or until --stop-file exists. SIGTERM/Ctrl-C finish the current
move properly (tracking back on, 'none' sent). Exits 2 after --lost frames without the planet.

    ASIAIR_HOST=192.168.1.36 python3 -u planethold.py --east=0.992,0.126 --arcsec-per-px 0.0998 --seconds 1800
"""
import argparse, os, signal, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from air_rpc import Air
from daypipes import Pipes, host

ap = argparse.ArgumentParser()
ap.add_argument("--east", required=True, help="sky east on the sensor 'x,y' (camangle)")
ap.add_argument("--arcsec-per-px", type=float, default=0.0998, help="bin-1 scale")
ap.add_argument("--tol", type=float, default=8.0, help="arcsec")
ap.add_argument("--every", type=float, default=3.0, help="seconds between frames")
ap.add_argument("--seconds", type=float, default=1800.0)
ap.add_argument("--stop-file", default="/tmp/planethold.stop")
ap.add_argument("--lost", type=int, default=5, help="consecutive frames without the planet -> exit 2")
ap.add_argument("--exp", type=float, default=0.02); ap.add_argument("--gain", type=int, default=300)
a = ap.parse_args()
E = np.array([float(v) for v in a.east.split(",")]); E /= np.linalg.norm(E); N = np.array([-E[1], E[0]])
def log(s): print("%s  %s" % (time.strftime("%H:%M:%S"), s), flush=True)
def _term(signum, _f): raise SystemExit(128 + signum)
signal.signal(signal.SIGTERM, _term)

def mdo(fn):
    m = Air(host(), 4400)
    try: return fn(m)
    finally: m.close()
def pulse(cmd, secs):
    def f(m):
        idx = m.call("scope_get_info", [])["result"]["slew_rate_index"]
        try: m.call("scope_set_slew_rate", [4]); m.call("scope_move", [cmd]); time.sleep(secs)
        finally: m.call("scope_move", ["none"]); m.call("scope_move", ["none"]); m.call("scope_set_slew_rate", [idx])
    mdo(f)
def move(e_as, n_as):
    if abs(n_as) > 5: pulse("south" if n_as > 0 else "north", min(abs(n_as) / 312.0, 0.5))   # pier west: 'south' raises Dec
    if e_as > 5:
        try: mdo(lambda m: m.call("scope_set_track_state", [False])); time.sleep(min(e_as / 15.0, 6.0))
        finally: mdo(lambda m: m.call("scope_set_track_state", [True]))
    elif e_as < -5: pulse("west", min(-e_as / 312.0, 0.5))

if os.path.exists(a.stop_file): os.remove(a.stop_file)
p = Pipes(); code = 0; t0 = time.time(); miss = 0; n = 0; moves = 0
try:
    p.setup("preview", a.exp, a.gain, 2); p.grab(timeout=40)
    log("holding: tol %.0f\", a frame every %.0f s, for %.0f s or until %s exists" % (a.tol, a.every, a.seconds, a.stop_file))
    while time.time() - t0 < a.seconds and not os.path.exists(a.stop_file):
        tf = time.time()
        img, w, h, _ = p.grab(timeout=40); img = img.astype(np.float64); n += 1
        med = np.median(img); sm = ndimage.gaussian_filter(img, 3); pk = sm.max()
        if pk - med < 1500:
            miss += 1; log("frame %d: planet NOT in the field (%d in a row)" % (n, miss))
            if miss >= a.lost: code = 2; break
            continue
        miss = 0
        lab, k = ndimage.label(sm > med + 0.25 * (pk - med)); j = int(np.argmax(ndimage.sum(np.ones_like(sm), lab, range(1, k + 1)))) + 1
        cy, cx = ndimage.center_of_mass(np.clip(sm - med, 0, None), lab, j)
        d = np.array([2 * cx - w, 2 * cy - h]); e_as, n_as = float(d @ E) * a.arcsec_per_px, float(d @ N) * a.arcsec_per_px
        act = ""
        if abs(e_as) > a.tol or abs(n_as) > a.tol:
            move(e_as, n_as); moves += 1; act = "  -> corrected"
        if n % 5 == 1 or act:
            log("frame %d (%.0f s): planet %+4.0f\" east %+4.0f\" north of centre%s" % (n, time.time() - t0, e_as, n_as, act))
        time.sleep(max(0.0, a.every - (time.time() - tf)))
finally:
    try: p.close()
    except Exception: pass
    try:
        mdo(lambda m: m.call("scope_move", ["none"]))
        if mdo(lambda m: m.call("scope_get_track_state", [])["result"]) is not True:
            mdo(lambda m: m.call("scope_set_track_state", [True])); log("tracking re-enabled")
    except Exception as e:
        log("!! could not confirm the mount state (%s) -- CHECK TRACKING" % e)
    log("hold ended after %.0f s: %d frames, %d corrections, exit %d" % (time.time() - t0, n, moves, code))
sys.exit(code)
