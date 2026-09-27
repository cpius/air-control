#!/usr/bin/env python3
"""One deep preview frame at the current pointing: source count, star sizes
(focus gauge), a PNG, and an on-Air plate solve with the register read
BEFORE the solve (the Air auto-syncs on success, so read first or the
pointing error is zero by construction)."""
import argparse, math, os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from daypipes import host, Pipes, log, save_png, focuser_pos
from findstar import blobs
from mount import Mount
from solving import solve

ap = argparse.ArgumentParser()
ap.add_argument("--exp", type=float, default=5.0)
ap.add_argument("--gain", type=int, default=300)
ap.add_argument("--bin", type=int, default=2)
ap.add_argument("--nsig", type=float, default=5.0)
ap.add_argument("--solve", action="store_true", help="plate solve the frame on the Air")
ap.add_argument("--solve-timeout", type=float, default=45.0)
ap.add_argument("--frames", type=int, default=1)
ap.add_argument("--tag", default="deep")
a = ap.parse_args()

m = Mount(host()); p = Pipes()
try:
    st = m.state(); st = m.state()
    log("register BEFORE: RA %.4f Dec %.4f Alt %.2f Az %.2f track=%s ; EAF %d" % (
        st["RA"], st["Dec"], st["Alt"], st["Az"], st["is_enable_track"], focuser_pos(p)))
    p.setup("preview", a.exp, a.gain, a.bin)
    for i in range(a.frames):
        t0 = time.time()
        img, w, h, info = p.grab(timeout=a.exp + 40)
        bg, sig, bl = blobs(img, nsig=a.nsig, top=30)
        fn = "frames/%s_%d.png" % (a.tag, i)
        save_png(img, fn, shrink=1)
        log("frame %d in %.1fs: %dx%d bg %.0f sigma %.1f max %.0f ; %d blobs >%g sigma -> %s" % (
            i, time.time() - t0, w, h, bg, sig, img.max(), len(bl), a.nsig, fn))
        for b in bl[:12]:
            log("   blob x=%5.0f y=%5.0f  flux %9.0f  peak %6.0f  area %4d spx  diam %5.1f  hfd %5.1f px(bin2)" % (
                b["x"], b["y"], b["flux"], b["peak"], b["area_spx"], b["diam_bin2"], b["hfd_bin2"]))
        if a.solve:
            t1 = time.time()
            r = solve(p.s.air, timeout=a.solve_timeout)
            if r is None:
                log("solve: NO SOLUTION in %.1fs" % (time.time() - t1))
            else:
                ra, dec = r["ra_dec"]
                log("solve OK in %.1fs: RA %.4fh Dec %+.4f  stars %s  fov %s  angle %s  image_id %s" % (
                    time.time() - t1, ra, dec, r.get("star_number"), r.get("fov"), r.get("angle"), r.get("image_id")))
                dra = (ra - st["RA"]) * 15 * math.cos(math.radians(dec)) * 60; ddec = (dec - st["Dec"]) * 60
                log("   pointing error (solved - register): dRA %+.1f'  dDec %+.1f'" % (dra, ddec))
                try:
                    st2 = m.state()
                except Exception:
                    m = Mount(host())   # 4400 drops idle sockets during a long solve
                    st2 = m.state()
                log("register AFTER: RA %.4f Dec %.4f (auto-sync moved it by %+.2f' RA, %+.2f' Dec)" % (
                    st2["RA"], st2["Dec"], (st2["RA"] - st["RA"]) * 15 * 60 * math.cos(math.radians(st2["Dec"])), (st2["Dec"] - st["Dec"]) * 60))
finally:
    p.close(); m.close(); log("closed")
