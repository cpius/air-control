#!/usr/bin/env python3
"""Find a bright star the register says we are on: step the goto along RA
(then Dec rows) in field-sized hops until a saturated blob appears, then sync."""
import argparse, math, os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daypipes import host, Pipes, log, save_png, move_to, focuser_pos
from findstar import STARS, jnow, blobs
from mount import Mount

ap = argparse.ArgumentParser()
ap.add_argument("--star", default="vega")
ap.add_argument("--step-arcmin", type=float, default=14.0)
ap.add_argument("--max-steps", type=int, default=8)
ap.add_argument("--dec-rows", default="0", help="comma list of Dec offsets in arcmin, e.g. 0,-20,20")
ap.add_argument("--exp", type=float, default=0.5)
ap.add_argument("--gain", type=int, default=100)
ap.add_argument("--sync", action="store_true")
ap.add_argument("--tag", default="line")
a = ap.parse_args()
ra0, de0 = jnow(*STARS[a.star])
m = Mount(host()); p = Pipes()
found = None
def goto_small(ra, dec):
    m.air.drain_events()
    r = m._r("scope_goto", [float(ra), float(dec)])
    t0 = time.time()
    while time.time() - t0 < 25:
        time.sleep(0.5)
        st = m.state()
        if abs(st["RA"] - ra) < 0.003 and abs(st["Dec"] - dec) < 0.02 and st["move_status"] == "none":
            return st, time.time() - t0
    return m.state(), time.time() - t0
try:
    p.setup("preview", a.exp, a.gain, 2)
    order = [0] + [s * k for k in range(1, a.max_steps + 1) for s in (1, -1)]
    rows = [float(x) for x in a.dec_rows.split(",")]
    n = 0
    for drow in rows:
        for k in order:
            dec = de0 + drow / 60.0
            ra = ra0 + k * a.step_arcmin / (60.0 * 15.0 * math.cos(math.radians(dec)))
            st, dt = goto_small(ra, dec)
            img, w, h, info = p.grab()
            bg, sig, bl = blobs(img, nsig=8.0, top=3)
            n += 1
            big = [b for b in bl if b["peak"] >= 60000 or b["flux"] > 2e5]
            top = bl[0] if bl else None
            log("#%2d RA %+d Dec %+.0f': goto %.1fs -> RA %.4f Dec %.3f (Alt %.1f) | bg %.0f sig %.0f max %.0f | %s" % (
                n, k, drow, dt, st["RA"], st["Dec"], st["Alt"], bg, sig, img.max(),
                ("top blob x=%.0f y=%.0f flux %.0f peak %.0f area %d" % (top["x"], top["y"], top["flux"], top["peak"], top["area_spx"])) if top else "no blobs"))
            if big:
                found = (k, drow, st, big[0])
                save_png(img, "frames/%s_found.png" % a.tag, shrink=2)
                log("FOUND: blob x=%.0f y=%.0f flux %.0f peak %.0f area %d spx at register RA %.4f Dec %.4f (offset %+d steps RA, %+.0f' Dec)" % (
                    big[0]["x"], big[0]["y"], big[0]["flux"], big[0]["peak"], big[0]["area_spx"], st["RA"], st["Dec"], k, drow))
                break
        if found:
            break
    if found and a.sync:
        # the star is at pixel (x,y) of a 1920x1080 bin2 frame; sync the register to the star's
        # coordinates only when it is near the centre (else the sync carries the offset)
        b = found[3]
        offx = (b["x"] - 960) * 0.588 / 60.0; offy = (b["y"] - 540) * 0.588 / 60.0
        log("star is %.1f' / %.1f' from frame centre (x/y); syncing register to %s at RA %.4f Dec %.4f" % (offx, offy, a.star, ra0, de0))
        log("scope_sync -> %s" % m.sync(ra0, de0))
        st = m.state(); log("register now RA %.4f Dec %.4f" % (st["RA"], st["Dec"]))
finally:
    p.close(); m.close(); log("closed")
