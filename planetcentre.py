#!/usr/bin/env python3
"""Centre a bright planet in the full field with pulses only (no goto: register not trustworthy).
Optional pre-move by a predicted drift, then up to --passes rounds of: preview frame -> centroid ->
Dec by a 20x pulse (312"/s), RA east by pausing tracking (15"/s), RA west by a pulse.
Exit 0 = centred within --tol, 2 = planet not in the full field, 1 = not converged.

    ASIAIR_HOST=192.168.1.36 python3 -u planetcentre.py --pre-east -169 --pre-north -480 --east=0.239,0.971
"""
import argparse, os, signal, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from air_rpc import Air
from daypipes import Pipes, host

ap = argparse.ArgumentParser()
ap.add_argument("--east", default="0.239,0.971", help="sky east on the sensor (camangle.py)")
ap.add_argument("--pre-east", type=float, default=0.0, help="arcsec to move the pointing east before looking (negative = west)")
ap.add_argument("--pre-north", type=float, default=0.0, help="arcsec to move the pointing north before looking")
ap.add_argument("--passes", type=int, default=3); ap.add_argument("--tol", type=float, default=12.0, help="arcsec")
ap.add_argument("--exp", type=float, default=0.02); ap.add_argument("--gain", type=int, default=300)
ap.add_argument("--arcsec-per-px", type=float, default=0.110)
ap.add_argument("--nudge", default="slow", choices=["slow", "fast"], help="slow = slowpulse.Nudger (1x, mount-timed dead-man, Dec backlash compensated; "
                "2026-09-28 Moon test), fast = the old 20x pulses + tracking pause")
a = ap.parse_args()
E = np.array([float(v) for v in a.east.split(",")]); E /= np.linalg.norm(E); N = np.array([-E[1], E[0]])
def log(s): print("%s  %s" % (time.strftime("%H:%M:%S"), s), flush=True)
def _term(signum, _f): raise SystemExit(128 + signum)      # satloop stops children with SIGTERM: finish any move first
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
    mdo(f); time.sleep(1.0)
nudger = None
def move(e_as, n_as):
    """move the POINTING by e_as east, n_as north (arcsec). Pier west: 'south' raises Dec."""
    global nudger
    if a.nudge == "slow":
        if nudger is None:
            from slowpulse import Nudger
            nudger = Nudger(host(), log=log)
        done = nudger.nudge(e_as, n_as); time.sleep(0.8)
        log("  nudged: " + (", ".join("%s %.0f\" at %dx" % (c, amt, 1 if r == 0 else 4) for c, amt, r, _ in done) or "nothing"))
        return
    if abs(n_as) > 8: pulse("south" if n_as > 0 else "north", min(abs(n_as) / 312.0, 4.0))
    if e_as > 8:
        try: mdo(lambda m: m.call("scope_set_track_state", [False])); time.sleep(min(e_as / 15.0, 30.0))
        finally: mdo(lambda m: m.call("scope_set_track_state", [True]))
        time.sleep(0.8)
    elif e_as < -8: pulse("west", min(-e_as / 312.0, 4.0))

if a.pre_east or a.pre_north:
    log("pre-move: %+.0f\" east %+.0f\" north" % (a.pre_east, a.pre_north)); move(a.pre_east, a.pre_north)
p = Pipes(); code = 1
try:
    p.setup("preview", a.exp, a.gain, 2); p.grab(timeout=40)
    for it in range(a.passes + 1):
        img, w, h, _ = p.grab(timeout=40); img = img.astype(np.float64)
        med = np.median(img); sm = ndimage.gaussian_filter(img, 3); pk = sm.max()
        if pk - med < 1500:
            log("planet NOT in the full field (peak %.0f over the sky)" % (pk - med)); code = 2; break
        lab, n = ndimage.label(sm > med + 0.25 * (pk - med)); k = int(np.argmax(ndimage.sum(np.ones_like(sm), lab, range(1, n + 1)))) + 1
        cy, cx = ndimage.center_of_mass(np.clip(sm - med, 0, None), lab, k)
        d = np.array([2 * cx - 1920, 2 * cy - 1080]); e_as, n_as = float(d @ E) * a.arcsec_per_px, float(d @ N) * a.arcsec_per_px
        log("pass %d: planet at sensor (%.0f,%.0f): %+.0f\" east %+.0f\" north of centre" % (it, 2 * cx, 2 * cy, e_as, n_as))
        if abs(e_as) < a.tol and abs(n_as) < a.tol:
            code = 0; break
        if it < a.passes: move(e_as, n_as)
finally:
    try: p.close()
    except Exception: pass
    st = mdo(lambda m: m.call("scope_get_track_state", [])["result"])
    if st is not True: mdo(lambda m: m.call("scope_set_track_state", [True])); log("tracking re-enabled")
log("exit %d" % code); sys.exit(code)
