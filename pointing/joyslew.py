#!/usr/bin/env python3
"""Closed-loop joystick slew to register RA/Dec -- for when scope_goto is dead (300).

    ASIAIR_HOST=192.168.1.36 python3 -u joyslew.py --ra 21.06 --dec -19.33 --min-alt 7

Drives one axis at a time with timed scope_move chunks (<= --chunk s, stop in a
finally), re-reading the register after every chunk. Nothing is assumed:
  * the direction->sign mapping is measured per axis on the first chunk and
    re-measured whenever pier_side changes (it flips -- mount-direction-sign-flips);
  * at the pole both Dec directions lower Dec, so the first Dec move picks the
    direction that RAISES altitude (over the zenith, not down into the north);
  * the rate is re-estimated from every chunk, not taken from the index;
  * a chunk that "moves" > 3x what the rate predicts (+0.5 deg) aborts: the
    register teleports in the dead-goto state (goto-is-broken-below-the-api);
  * altitude below --min-alt aborts; so does --max-seconds of wall clock.
Dec first (big sweeps along the meridian), then RA. Coarse at MAX/2, fine at 20x.
"""
import argparse, math, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from joystick import Joystick

ap = argparse.ArgumentParser()
ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"))
ap.add_argument("--ra", type=float, required=True, help="target register RA, hours (JNow)")
ap.add_argument("--dec", type=float, required=True, help="target register Dec, degrees")
ap.add_argument("--tol", type=float, default=2.0, help="arrival tolerance per axis, arcmin")
ap.add_argument("--min-alt", type=float, default=7.0, help="abort below this register altitude")
ap.add_argument("--chunk", type=float, default=3.0, help="longest single scope_move, s")
ap.add_argument("--coarse-rate", type=int, default=6, help="slew-rate index for big moves (6=MAX/2)")
ap.add_argument("--fine-rate", type=int, default=4, help="slew-rate index for the last --fine-below deg (4=20x)")
ap.add_argument("--fine-below", type=float, default=0.4, help="switch to the fine rate below this error, deg")
ap.add_argument("--max-seconds", type=float, default=300.0)
a = ap.parse_args()

T0 = time.time()
def log(s): print("%s +%5.1fs %s" % (time.strftime("%H:%M:%S"), time.time() - T0, s), flush=True)

def errs(st):
    dra_h = ((a.ra - st["RA"] + 12.0) % 24.0) - 12.0
    return dra_h * 15.0 * math.cos(math.radians(a.dec)), a.dec - st["Dec"]   # on-sky deg

j = Joystick(a.host)
AX = {"dec": ("north", "south"), "ra": ("east", "west")}
sign = {}              # (axis, pier) -> +1 if AX[axis][0] increases the coordinate
rate = {}              # rate index -> deg/s estimate
cur_rate = [None]

def set_rate(idx):
    if cur_rate[0] != idx:
        j.set_rate(idx); time.sleep(0.3); cur_rate[0] = idx

def show(st, tag=""):
    e_ra, e_dec = errs(st)
    log("%-10s RA %.4fh Dec %+8.3f  Alt %5.2f Az %6.2f  pier %-7s err RA %+7.1f' Dec %+7.1f'"
        % (tag, st["RA"], st["Dec"], st["Alt"], st["Az"], st["pier_side"], e_ra * 60, e_dec * 60))

def chunk(axis, direction, secs, idx):
    set_rate(idx)
    b = j.state()
    j.move(direction)
    t = time.time()
    try:
        while time.time() - t < secs:
            time.sleep(0.02)
    finally:
        j.stop()
    time.sleep(0.6)
    s = j.state()
    if s["Alt"] < a.min_alt:
        show(s, "ABORT")
        raise SystemExit("altitude %.2f below --min-alt %.1f -- stopped" % (s["Alt"], a.min_alt))
    d_ra = (((s["RA"] - b["RA"] + 12) % 24) - 12) * 15 * math.cos(math.radians(s["Dec"]))
    moved = s["Dec"] - b["Dec"] if axis == "dec" else d_ra
    other = d_ra if axis == "dec" else s["Dec"] - b["Dec"]
    exp = rate.get(idx)
    if exp and abs(moved) > 3 * exp * secs + 0.5:
        show(s, "JUMP?")
        raise SystemExit("register moved %.2f deg for an expected %.2f -- register jump, aborting"
                         % (moved, exp * secs))
    r = abs(moved) / secs
    rate[idx] = r if idx not in rate else 0.5 * rate[idx] + 0.5 * r
    log("  %s %-5s %.2fs @%d: moved %+.3f deg (cross-axis %+.3f), rate ~%.3f deg/s"
        % (axis, direction, secs, idx, moved, other, rate[idx]))
    return b, s, moved

def drive(axis):
    while True:
        if time.time() - T0 > a.max_seconds:
            raise SystemExit("wall-clock cap %.0fs reached" % a.max_seconds)
        st = j.state()
        e_ra, e_dec = errs(st)
        err = e_dec if axis == "dec" else e_ra
        show(st, axis)
        if abs(err) * 60 < a.tol:
            return st
        idx = a.coarse_rate if abs(err) > a.fine_below else a.fine_rate
        key = (axis, st["pier_side"])
        if key not in sign:
            probe = AX[axis][0]
            if axis == "dec" and st["Dec"] > 88.0:
                # pole: both directions lower Dec; the right one raises Alt
                b, s, moved = chunk(axis, probe, 1.0, idx)
                if s["Alt"] < b["Alt"] and err < 0:
                    log("  '%s' from the pole LOWERS alt (toward the north horizon) -- using '%s'"
                        % (probe, AX[axis][1]))
                    chunk(axis, AX[axis][1], 1.0, idx)      # undo
                    sign[key] = +1          # so err<0 picks AX[1] below
                else:
                    sign[key] = -1 if moved < 0 else +1
                    if err < 0: sign[key] = -1   # probe lowered Dec and raised alt: keep it
                continue
            b, s, moved = chunk(axis, probe, 1.0, idx)
            if abs(moved) < 1e-4:
                raise SystemExit("%s '%s' moved nothing -- mount not responding" % (axis, probe))
            sign[key] = 1 if moved > 0 else -1
            log("  sign: '%s' %s %s at pier %s" % (probe, "raises" if moved > 0 else "lowers",
                                                   axis.upper(), st["pier_side"]))
            continue
        want_up = err > 0
        direction = AX[axis][0] if (sign[key] > 0) == want_up else AX[axis][1]
        r = rate.get(idx)
        secs = a.chunk if not r else max(0.3, min(a.chunk, 0.85 * abs(err) / r))
        b, s, moved = chunk(axis, direction, secs, idx)
        if abs(moved) > 1e-4 and (moved > 0) != want_up:
            log("  WRONG WAY (%+.3f deg) -- re-measuring sign" % moved)
            sign.pop(key, None)

try:
    show(j.state(), "start")
    if not j.state().get("is_enable_track"):
        log("WARNING: tracking is OFF")
    drive("dec")
    drive("ra")
    st = drive("dec")          # RA moves leak a little Dec
    show(st, "ARRIVED")
finally:
    j.close()
