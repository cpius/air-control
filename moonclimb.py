#!/usr/bin/env python3
"""Climb the Moon's near-limb glare into the disc (for when the field is bright
but shows no surface: the disc is within ~10').

    ASIAIR_HOST=192.168.1.36 python3 -u moonclimb.py --exp-ms 20 --gain 100 --step 5

Level = clipped mean minus bias (preview, bin 2, 16-bit). Each axis is probed
with one --step arcmin nudge; the direction that raises the level is kept and
repeated while the level keeps rising; axes alternate. Stops when the frame is
on the disc (level > --disc) or shows the limb (bright fraction between 5% and
95%), or after --max-steps. Everything is measured, no Jacobian assumed.
"""
import argparse, math, os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daypipes import host, Pipes, log, save_png
from joystick import Joystick

ap = argparse.ArgumentParser()
ap.add_argument("--exp-ms", type=float, default=20.0); ap.add_argument("--gain", type=int, default=100)
ap.add_argument("--step", type=float, default=5.0, help="arcmin per nudge")
ap.add_argument("--rate", type=int, default=4, help="slew-rate index (4=20x ~5'/s)")
ap.add_argument("--disc", type=float, default=1500.0, help="level (ADU over bias) that means lunar surface fills the frame")
ap.add_argument("--max-steps", type=int, default=16)
ap.add_argument("--min-alt", type=float, default=7.0)
ap.add_argument("--outdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "telemetry", time.strftime("%Y-%m-%d"), "moonclimb"))
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)

j = Joystick(host()); p = Pipes()

def cmean(img):
    lo, hi = np.percentile(img, [0.5, 99.5]); return float(img[(img >= lo) & (img <= hi)].mean())

def shoot(tag):
    img, w, h, info = p.grab(timeout=40)
    lvl = cmean(img) - BIAS
    third = w // 3
    gx = float(img[:, -third:].mean() - img[:, :third].mean()); gy = float(img[-h // 3:, :].mean() - img[:h // 3, :].mean())
    frac = float((img - BIAS > a.disc).mean())
    fn = save_png(img, os.path.join(a.outdir, "%s_%s.png" % (time.strftime("%H%M%S"), tag)), shrink=2)
    log("  %-10s level %7.1f  grad x %+7.1f y %+7.1f  bright-frac %.3f  max %5.0f  %s" % (tag, lvl, gx, gy, frac, img.max(), os.path.basename(fn)))
    return lvl, frac, gx, gy

def rate_secs():
    return a.step / 60.0 / 0.085          # index 4 ~ 0.085 deg/s measured tonight

def nudge(d):
    s = j.state()
    if s["Alt"] < a.min_alt:
        raise SystemExit("alt %.2f below --min-alt" % s["Alt"])
    j.nudge(d, rate_secs())
    time.sleep(0.5)

def done(lvl, frac):
    if lvl > a.disc:
        log("ON THE DISC: level %.0f over bias" % lvl); return True
    if 0.05 < frac < 0.95:
        log("LIMB IN FRAME: %.0f%% of pixels are lunar surface" % (100 * frac)); return True
    return False

try:
    j.set_rate(a.rate); time.sleep(0.2)
    p.setup("preview", 0.001, a.gain, 2)
    img, *_ = p.grab(timeout=40); BIAS = cmean(img); log("bias %.0f" % BIAS)
    p.setup("preview", a.exp_ms / 1000.0, a.gain, 2)
    s = j.state(); log("start register RA %.4f Dec %+.3f Alt %.2f Az %.2f pier %s" % (s["RA"], s["Dec"], s["Alt"], s["Az"], s["pier_side"]))
    best, frac, gx, gy = shoot("start")
    if done(best, frac): sys.exit(0)
    axes = {"dec": ("north", "south"), "ra": ("east", "west")}
    good = {}                                    # axis -> direction that raised the level
    steps = 0
    # the x gradient is usually the bigger one and Dec moves x on this camera angle; probe the axis
    order = ["dec", "ra"] if abs(gx) >= abs(gy) else ["ra", "dec"]
    while steps < a.max_steps:
        progressed = False
        for axis in order:
            if axis not in good:
                d0, d1 = axes[axis]
                nudge(d0); steps += 1
                lvl, frac, gx, gy = shoot("%s+%g" % (d0, a.step))
                if done(lvl, frac): sys.exit(0)
                if lvl > best * 1.03 + 1:
                    good[axis] = d0; best = lvl; progressed = True
                    log("  %s: '%s' brightens" % (axis, d0))
                else:
                    nudge(d1); nudge(d1); steps += 2
                    lvl, frac, gx, gy = shoot("%s+%g" % (d1, a.step))
                    if done(lvl, frac): sys.exit(0)
                    if lvl > best * 1.03 + 1:
                        good[axis] = d1; best = lvl; progressed = True
                        log("  %s: '%s' brightens" % (axis, d1))
                    else:
                        nudge(d0); steps += 1          # back to where we were
                        log("  %s: neither direction brightens -- axis parked" % axis)
                        good[axis] = None
                continue
            if good[axis] is None:
                continue
            # keep going while it rises
            nudge(good[axis]); steps += 1
            lvl, frac, gx, gy = shoot("%s+%g" % (good[axis], a.step))
            if done(lvl, frac): sys.exit(0)
            if lvl > best * 1.03 + 1:
                best = lvl; progressed = True
            else:
                log("  %s: '%s' stopped rising (%.0f vs %.0f) -- re-probe next round" % (axis, good[axis], lvl, best))
                good.pop(axis)
        if not progressed and all(v is None for v in good.values()) and len(good) == 2:
            log("no direction brightens -- stopping"); break
    s = j.state(); log("END register RA %.4f Dec %+.3f Alt %.2f Az %.2f after %d nudges, level %.0f" % (s["RA"], s["Dec"], s["Alt"], s["Az"], steps, best))
finally:
    j.close()
