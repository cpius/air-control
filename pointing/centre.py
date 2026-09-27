#!/usr/bin/env python3
"""Centre the brightest star with two calibration gotos (a measured 2x2
Jacobian from register offsets to pixels), then sync the register to the
star's catalogue position. Assumes goto works (it did tonight, 2026-09-10)."""
import argparse, math, os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from daypipes import host, Pipes, log, save_png
from findstar import STARS, jnow, blobs
from mount import Mount

def star_xy(p):
    img, w, h, info = p.grab()
    bg, sig, bl = blobs(img, nsig=8.0, top=1)
    if not bl:
        return None, img
    return (bl[0]["x"], bl[0]["y"], bl[0]["peak"], bl[0]["flux"]), img

def goto_small(m, ra, dec, timeout=25):
    m.air.drain_events(); m._r("scope_goto", [float(ra), float(dec)]); t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.5); st = m.state()
        if abs(st["RA"] - ra) < 0.003 and abs(st["Dec"] - dec) < 0.02 and st["move_status"] == "none":
            return st
    return m.state()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--star", default="vega", help="name from STARS, or use --ra/--dec")
    ap.add_argument("--ra", type=float, default=None, help="JNow hours (planets: from Horizons), overrides --star")
    ap.add_argument("--dec", type=float, default=None, help="JNow degrees")
    ap.add_argument("--exp", type=float, default=0.05)
    ap.add_argument("--gain", type=int, default=0)
    ap.add_argument("--cal-arcmin", type=float, default=3.0)
    ap.add_argument("--tol-px", type=float, default=25.0)
    ap.add_argument("--no-sync", action="store_true")
    ap.add_argument("--target", default="960,540", help="bin2 pixel to put the star on")
    ap.add_argument("--jacobian", default=None, help="px/arcmin as a,b,c,d for [[dx/dRA, dx/dDec],[dy/dRA, dy/dDec]] -- skips the calibration moves")
    a = ap.parse_args()
    ra0, de0 = (a.ra, a.dec) if a.ra is not None else jnow(*STARS[a.star]); tx, ty = [float(v) for v in a.target.split(",")]
    m = Mount(host()); p = Pipes()
    try:
        p.setup("preview", a.exp, a.gain, 2)
        st = m.state(); ra, dec = st["RA"], st["Dec"]
        s0, _ = star_xy(p)
        if not s0:
            log("no star in the frame -- probing +/-3' offsets")
            for sra, sde in ((0, -1), (0, 1), (-1, 0), (1, 0), (1, 1), (-1, -1), (1, -1), (-1, 1)):
                dra_p = sra * a.cal_arcmin / (60.0 * 15.0 * math.cos(math.radians(dec))); dde_p = sde * a.cal_arcmin / 60.0
                goto_small(m, ra + dra_p, dec + dde_p); s0, _ = star_xy(p)
                log("  offset RA %+d Dec %+d -> %s" % (sra, sde, ("star at x=%.0f y=%.0f" % (s0[0], s0[1])) if s0 else "nothing"))
                if s0:
                    ra, dec = ra + dra_p, dec + dde_p
                    break
            if not s0:
                sys.exit("no star found around here")
        log("star at x=%.0f y=%.0f (peak %.0f) ; register RA %.4f Dec %.4f" % (s0[0], s0[1], s0[2], ra, dec))
        dra = a.cal_arcmin / (60.0 * 15.0 * math.cos(math.radians(dec))); ddec = a.cal_arcmin / 60.0
        if a.jacobian:
            J = np.array([float(v) for v in a.jacobian.split(",")]).reshape(2, 2)
            log("using given Jacobian px/arcmin: %s" % J.tolist())
            cur_ra, cur_dec = ra, dec; cur = s0
        else:
          goto_small(m, ra + dra, dec); s1, _ = star_xy(p)
          if not s1: sys.exit("lost the star after the RA calibration move")
          log("after RA %+.1f': x=%.0f y=%.0f  (dx %+.0f dy %+.0f)" % (a.cal_arcmin, s1[0], s1[1], s1[0]-s0[0], s1[1]-s0[1]))
          s2 = None
          for dsign in (-1, 1):
              goto_small(m, ra + dra, dec + dsign * ddec); s2, _ = star_xy(p)
              if s2:
                  break
              log("  Dec %+.1f' lost the star; going back and trying the other way" % (dsign * a.cal_arcmin))
              goto_small(m, ra + dra, dec)
          if not s2: sys.exit("lost the star after the Dec calibration move")
          log("after Dec %+.1f': x=%.0f y=%.0f  (dx %+.0f dy %+.0f)" % (dsign * a.cal_arcmin, s2[0], s2[1], s2[0]-s1[0], s2[1]-s1[1]))
          J = np.array([[s1[0]-s0[0], (s2[0]-s1[0]) * dsign], [s1[1]-s0[1], (s2[1]-s1[1]) * dsign]]) / a.cal_arcmin   # px per arcmin
          log("Jacobian px/arcmin: %s ; scale %.1f px/arcmin (expect ~102 at 0.588\"/px)" % (J.round(1).tolist(), math.sqrt(abs(np.linalg.det(J)))))
          cur_ra, cur_dec = ra + dra, dec + dsign * ddec; cur = s2
        for it in range(3):
            need = np.array([tx - cur[0], ty - cur[1]])
            if np.hypot(*need) < a.tol_px:
                break
            corr = np.linalg.solve(J, need)          # arcmin in (RA, Dec)
            cur_ra += corr[0] / (60.0 * 15.0 * math.cos(math.radians(cur_dec))); cur_dec += corr[1] / 60.0
            goto_small(m, cur_ra, cur_dec); s, img = star_xy(p)
            if not s: sys.exit("lost the star during centring")
            log("iter %d: moved RA %+.2f' Dec %+.2f' -> star x=%.0f y=%.0f (residual %.0f px)" % (it, corr[0], corr[1], s[0], s[1], math.hypot(tx - s[0], ty - s[1])))
            got = np.array([s[0] - cur[0], s[1] - cur[1]])
            if np.hypot(*got) > 60 and np.hypot(*corr) > 0.5:
                # rescale J so that it would have predicted this move exactly (rank-1 update along corr)
                pred = J @ corr; ratio = np.dot(got, pred) / max(np.dot(pred, pred), 1e-9)
                if 0.5 < ratio < 2.0:
                    J = J * ratio
            cur = s
        save_png(img if 'img' in dir() else _, "frames/centred.png", shrink=2)
        if not a.no_sync:
            log("scope_sync to %s (RA %.4f Dec %.4f) -> %s" % (a.star, ra0, de0, m.sync(ra0, de0)))
        st = m.state(); log("register RA %.4f Dec %.4f Alt %.2f Az %.2f track=%s" % (st["RA"], st["Dec"], st["Alt"], st["Az"], st["is_enable_track"]))
    finally:
        p.close(); m.close(); log("closed")
