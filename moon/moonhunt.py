#!/usr/bin/env python3
"""Find the Moon when pointing is off: climb the scattered-moonlight gradient.

    ASIAIR_HOST=192.168.1.36 python3 -u moon/moonhunt.py --step 30 --exp 1 --gain 200

At each position take one preview frame (bin 2) and score it by the median
minus the bias measured at --bias-exp. Probe the four neighbours at --step
arcmin (register offsets, joystick-driven, closed on the register), move to
the brightest if it beats the centre by --gain-frac, otherwise halve the step.
Stops when a frame is mostly lunar surface (median > --moon-adu over bias) or
the step drops below --min-step. Every frame is saved as a PNG.
"""
import argparse, math, os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from daypipes import Pipes, log, save_png
from joystick import Joystick

ap = argparse.ArgumentParser()
ap.add_argument("--step", type=float, default=30.0, help="first probe step, arcmin")
ap.add_argument("--min-step", type=float, default=4.0)
ap.add_argument("--exp", type=float, default=1.0); ap.add_argument("--gain", type=int, default=200)
ap.add_argument("--bias-exp", type=float, default=0.001)
ap.add_argument("--gain-frac", type=float, default=0.10, help="neighbour must beat centre by this fraction")
ap.add_argument("--moon-adu", type=float, default=3000.0, help="clipped mean over bias that means 'on the Moon'")
ap.add_argument("--min-alt", type=float, default=8.0)
ap.add_argument("--rate", type=int, default=5, help="slew-rate index for the moves (5=60x)")
ap.add_argument("--start-ra", type=float, help="register RA (h) to start from; default: where it is")
ap.add_argument("--start-dec", type=float)
ap.add_argument("--max-probes", type=int, default=40)
ap.add_argument("--outdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "telemetry", time.strftime("%Y-%m-%d"), "moonhunt"))
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)

j = Joystick(os.environ["ASIAIR_HOST"])
p = Pipes()
sign = {}          # axis -> direction name that INCREASES the register coordinate
rate = {"dec": 0.26, "ra": 0.26}   # deg/s, refined per move

def st(): return j.state()

def reg_err(s, t):
    dra = ((t[0] - s["RA"] + 12) % 24 - 12) * 15 * math.cos(math.radians(s["Dec"])) * 60
    return dra, (t[1] - s["Dec"]) * 60           # arcmin on sky

def move_to(t, tol=1.5):
    """Closed-loop joystick move to register target t=(ra_h, dec_d)."""
    j.set_rate(a.rate); time.sleep(0.2)
    for _ in range(12):
        s = st()
        if s["Alt"] < a.min_alt:
            raise SystemExit("alt %.2f below %.1f -- stopping" % (s["Alt"], a.min_alt))
        e = dict(zip(("ra", "dec"), reg_err(s, t)))
        axis = max(e, key=lambda k: abs(e[k]))
        if abs(e[axis]) < tol:
            return s
        names = ("east", "west") if axis == "ra" else ("north", "south")
        if axis not in sign:
            d0, d1, (b, c) = names[0], names[1], (None, None)
            dra, ddec, b, c = j.nudge(d0, 0.4)
            moved = ddec if axis == "dec" else dra
            sign[axis] = d0 if moved > 0 else d1
            rate[axis] = max(abs(moved) / 0.4, 0.02)
            log("  %s: '%s' raises the register at %.3f deg/s" % (axis, sign[axis], rate[axis]))
            continue
        up = sign[axis]; down = names[1] if up == names[0] else names[0]
        secs = max(0.25, min(3.0, abs(e[axis]) / 60 * 0.85 / rate[axis]))
        dra, ddec, b, c = j.nudge(up if e[axis] > 0 else down, secs)
        moved = abs(ddec if axis == "dec" else dra)
        if moved > 0.01:
            rate[axis] = 0.5 * rate[axis] + 0.5 * moved / secs
    return st()

def cmean(img):
    """Mean between the 0.5 and 99.5 percentiles: the 12-bit data arrive in
    16-ADU steps, so a median cannot see a sky gradient of a few ADU; this
    mean over ~2M pixels resolves ~0.1 ADU and ignores stars and hot pixels."""
    lo, hi = np.percentile(img, [0.5, 99.5])
    return float(img[(img >= lo) & (img <= hi)].mean())

def shoot(tag):
    img, w, h, info = p.grab(timeout=40)
    lvl = cmean(img) - BIAS
    fn = save_png(img, "%s/%s_%s.png" % (a.outdir, time.strftime("%H%M%S"), tag), shrink=2)
    log("   frame %-10s mean-bias %8.2f  p99-bias %8.1f  max %6.0f  %s" % (
        tag, lvl, float(np.percentile(img, 99)) - BIAS, img.max(), os.path.basename(fn)))
    return lvl

try:
    p.setup("preview", a.bias_exp, a.gain, 2)
    img, *_ = p.grab(timeout=40); BIAS = cmean(img)
    log("bias at %.3fs g%d: %.0f" % (a.bias_exp, a.gain, BIAS))
    p.setup("preview", a.exp, a.gain, 2)
    if a.start_ra is not None:
        move_to((a.start_ra, a.start_dec))
    s = st(); here = (s["RA"], s["Dec"])
    # the Moon moves against the register only slowly with Lunar tracking on
    log("start register RA %.4f Dec %+.3f Alt %.2f Az %.2f" % (s["RA"], s["Dec"], s["Alt"], s["Az"]))
    best = shoot("centre"); step = a.step; probes = 0
    while step >= a.min_step and probes < a.max_probes:
        if best > a.moon_adu:
            log("ON THE MOON: mean %.0f over bias" % best); break
        cands = []
        cosd = math.cos(math.radians(here[1]))
        for tag, dra, ddec in (("N", 0, step), ("S", 0, -step), ("E", step, 0), ("W", -step, 0)):
            t = (here[0] + dra / 60 / 15 / cosd, here[1] + ddec / 60)
            s = move_to(t); probes += 1
            lvl = shoot("%s%g" % (tag, step))
            cands.append((lvl, t, tag))
            if lvl > a.moon_adu: break
        lvl, t, tag = max(cands)
        if lvl > best * (1 + a.gain_frac) and lvl - best > 0.5:
            log("-> %s is brighter (%.1f vs %.1f): moving there, step stays %g'" % (tag, lvl, best, step))
            here, best = t, lvl
        else:
            log("no neighbour brighter than centre (%.1f): halving step %g' -> %g'" % (best, step, step / 2))
            step /= 2
        move_to(here)
    s = st()
    log("END register RA %.4f Dec %+.3f Alt %.2f Az %.2f, level %.1f, step %g'" % (s["RA"], s["Dec"], s["Alt"], s["Az"], best, step))
finally:
    j.close()
