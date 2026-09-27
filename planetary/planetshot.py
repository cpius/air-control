#!/usr/bin/env python3
"""One well-exposed single frame of a planet: exposure test on the brightest
blob (peak -> --target of full well), then a bin-1 preview frame, saved as
16-bit .npy + full PNG + a crop around the planet.

    ASIAIR_HOST=192.168.1.36 python3 -u planetshot.py --name saturn --exp-ms 60 --gain 300
"""
import argparse, os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daypipes import host, Pipes, log, save_png, focuser_pos
from findstar import blobs
from mount import Mount

ap = argparse.ArgumentParser()
ap.add_argument("--name", default="planet")
ap.add_argument("--exp-ms", type=float, default=60.0); ap.add_argument("--gain", type=int, default=300)
ap.add_argument("--target", type=float, default=0.6, help="fraction of full well for the peak")
ap.add_argument("--exp-max", type=float, default=500.0); ap.add_argument("--exp-min", type=float, default=2.0)
ap.add_argument("--bin", type=int, default=1)
ap.add_argument("--crop", type=int, default=600, help="crop size (bin-1 px) around the planet")
ap.add_argument("--outdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "telemetry", time.strftime("%Y-%m-%d"), "shots"))
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)
FULL = 65535.0

p = Pipes()
try:
    try:
        m = Mount(host()); s = m.state(); m.close()
        log("register RA %.4f Dec %+.3f Alt %.2f Az %.2f track %s ; EAF %d" % (s["RA"], s["Dec"], s["Alt"], s["Az"], s["is_enable_track"], focuser_pos(p)))
    except Exception as e:
        log("mount read failed: %s" % e)
    exp = a.exp_ms
    p.setup("preview", exp / 1000.0, a.gain, a.bin)
    best = None
    for it in range(6):
        img, w, h, info = p.grab(timeout=40)
        bg, sig, bl = blobs(img, nsig=8.0, top=3)
        big = [b for b in bl if b["area_spx"] >= 30]
        if not big:
            log("test %d: %.1f ms g%d -> no planet-sized blob (bg %.0f, %d blobs)" % (it, exp, a.gain, bg, len(bl)))
            best = None; break
        b = big[0]; peak = b["peak"]
        log("test %d: %.1f ms g%d -> planet at (%.0f, %.0f) peak %.0f (%.0f%% FW) area %d" % (it, exp, a.gain, b["x"], b["y"], peak, 100 * peak / FULL, b["area_spx"]))
        best = (img, b)
        if 0.8 * a.target * FULL <= peak <= min(1.15 * a.target * FULL, 0.95 * FULL):
            break
        scale = a.target * FULL / max(peak - bg, 1.0)
        if peak >= 0.98 * FULL:
            scale = 0.4
        new = max(a.exp_min, min(a.exp_max, exp * scale))
        if abs(new - exp) < 0.5:
            break
        exp = new
        p.c("set_control_value", ["Exposure", int(exp * 1000)]); p.exp = exp / 1000.0
        p.s.air.drain_events()
    if best is None:
        sys.exit("no planet in the frame")
    img, b = best
    stamp = time.strftime("%H%M%S")
    base = os.path.join(a.outdir, "%s_%s_%gms_g%d_bin%d" % (stamp, a.name, round(exp, 1), a.gain, a.bin))
    np.save(base + ".npy", img.astype(np.uint16))
    full_png = save_png(img, base + "_full.png", shrink=2 if a.bin == 1 else 1)
    x, y, c = int(b["x"]), int(b["y"]), a.crop // 2
    crop = img[max(0, y - c):y + c, max(0, x - c):x + c]
    crop_png = save_png(crop, base + "_crop.png", shrink=1)
    log("SHOT %s: %.1f ms g%d bin %d, planet at (%d, %d) peak %.0f (%.0f%% FW) ; %s ; %s ; %s" % (
        a.name, exp, a.gain, a.bin, x, y, b["peak"], 100 * b["peak"] / FULL, base + ".npy", full_png, crop_png))
finally:
    try: p.close()
    except Exception: pass
