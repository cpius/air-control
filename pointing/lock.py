#!/usr/bin/env python3
"""Hold a bright extended target (the Moon, a planet) centred, closed-loop on the frame.

Plate solving is useless on the Moon -- it drowns the star field -- and the
pointing model on an unaligned mount is worth degrees, so the only reliable
reference is the target itself. This measures the pixel<->mount mapping with two
deliberate nudges, inverts it, and then keeps the lit centroid on the frame
centre.

Two things this exists to survive:

  * Tracking silently switching OFF. At sidereal that is 15 arcsec/s -- 15'/min,
    so a 17' frame empties in about a minute and every symptom looks like bad
    pointing. Checked and re-asserted on every pass.
  * scope_move's `speed` argument being a blunt 12.4 deg instrument. Fine control
    comes from `scope_set_slew_rate` -- index 4 (20x) measures 5.46 arcmin/s.

    python3 pointing/lock.py                 # calibrate, then hold until Ctrl-C
    python3 pointing/lock.py --once          # single centring pass
    python3 pointing/lock.py --shoot out.png # centre, then save a frame
"""
import argparse, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import numpy as np
from air_rpc import Air
from starhunt import Camera
from airlog import add_log_args, configure_logging, get_logger

log = get_logger("lock")
KEY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "embedded_key.pem")
RATE_INDEX = 4          # 20x
ARCMIN_PER_S = 5.46     # measured for index 4
LIT = 3000              # ADU above which a pixel is lunar surface, not sky


class Lock:
    def __init__(self, host, key, exp, gain):
        self.host = host
        self.mount = Air(host, 4400, timeout=8)
        self.mount.call("scope_set_slew_rate", [RATE_INDEX], timeout=8)
        self.cam = Camera(host, key, cam_name="ZWO ASI585MC Air")
        self.exp, self.gain = exp, gain
        self.J = None       # [[dx/dN, dx/dE],[dy/dN, dy/dE]] pixels per arcmin

    # --- mount ---
    def tracking_on(self):
        i = self.mount.call("scope_get_info", [], timeout=8)["result"]
        if not i.get("is_enable_track"):
            log.warn("tracking was OFF -- re-asserting (that is 15 arcmin/min of drift)")
            self.mount.call("scope_set_track_state", [True], timeout=8)
        return i

    def nudge(self, d, arcmin):
        t = max(0.15, abs(arcmin) / ARCMIN_PER_S)
        self.mount.call("scope_move", [d], timeout=8)
        t0 = time.time()
        while time.time() - t0 < t:
            time.sleep(0.02)
        self.mount.call("scope_move", ["none"], timeout=8)
        time.sleep(0.5)

    # --- camera ---
    def frame(self):
        v, w, h, hdr = self.cam.grab(self.exp, self.gain)
        return np.frombuffer(v.tobytes(), dtype="<u2").astype(np.float32).reshape(h, w)

    def centroid(self):
        """Centre of the lit region, and how much of the frame it fills."""
        x = self.frame()
        h, w = x.shape
        m = x > LIT
        frac = m.mean()
        if frac < 0.005:
            return None, frac, (w, h), x
        ys, xs = np.nonzero(m)
        # bounding-box centre beats the mean when the disc runs off an edge
        return ((xs.min() + xs.max()) / 2.0, (ys.min() + ys.max()) / 2.0), frac, (w, h), x

    # --- the mapping ---
    def calibrate(self, step=4.0):
        c0, f0, (w, h), _ = self.centroid()
        if c0 is None:
            raise RuntimeError(f"nothing lit in frame ({f0*100:.2f}%) -- centre it first")
        self.nudge("north", step)
        c1, _, _, _ = self.centroid()
        self.nudge("south", step)
        self.nudge("east", step)
        c2, _, _, _ = self.centroid()
        self.nudge("west", step)
        if c1 is None or c2 is None:
            raise RuntimeError("target left the frame during calibration -- use a smaller --step")
        self.J = np.array([[(c1[0] - c0[0]) / step, (c2[0] - c0[0]) / step],
                           [(c1[1] - c0[1]) / step, (c2[1] - c0[1]) / step]])
        log.info("pixel/arcmin mapping: north->(%+.1f,%+.1f)  east->(%+.1f,%+.1f)",
                 self.J[0, 0], self.J[1, 0], self.J[0, 1], self.J[1, 1])
        if abs(np.linalg.det(self.J)) < 1.0:
            raise RuntimeError("mapping is degenerate -- both nudges moved the same way")

    def centre_once(self, tol_px=120):
        self.tracking_on()
        c, frac, (w, h), x = self.centroid()
        if c is None:
            log.warn("target not in frame (%.2f%% lit)", frac * 100)
            return False, frac, x
        err = np.array([w / 2.0 - c[0], h / 2.0 - c[1]])
        if np.hypot(*err) < tol_px:
            log.info("centred: off by %.0f px, %.1f%% lit", np.hypot(*err), frac * 100)
            return True, frac, x
        move = np.linalg.solve(self.J, err)      # arcmin: [north, east]
        log.info("off by (%+.0f,%+.0f) px -> north %+.2f' east %+.2f'",
                 err[0], err[1], move[0], move[1])
        for amt, pos, neg in ((move[0], "north", "south"), (move[1], "east", "west")):
            if abs(amt) > 0.3:
                self.nudge(pos if amt > 0 else neg, min(abs(amt), 20.0))
        return False, frac, x

    def close(self):
        try:
            self.mount.call("scope_move", ["none"], timeout=6)
        finally:
            self.cam.close()
            self.mount.close()


def save_png(x, path):
    import cv2
    rgb = cv2.cvtColor(x.astype(np.uint16), cv2.COLOR_BayerBG2RGB).astype(np.float32)
    m = rgb.mean(axis=2) > LIT
    if m.any():
        means = [rgb[..., i][m].mean() for i in range(3)]
        tgt = float(np.mean(means))
        for i in range(3):
            rgb[..., i] *= tgt / means[i]
        lo, hi = np.percentile(rgb.mean(axis=2)[m], (0.5, 99.7))
    else:
        lo, hi = np.percentile(rgb, (1, 99))
    png = (np.clip((rgb - lo) / (hi - lo), 0, 1) ** (1 / 1.7) * 255).astype(np.uint8)
    cv2.imwrite(path, cv2.cvtColor(png, cv2.COLOR_RGB2BGR))
    log.info("wrote %s", path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ)
    ap.add_argument("--key", default=KEY)
    ap.add_argument("--exp", type=float, default=0.03)
    ap.add_argument("--gain", type=int, default=0)
    ap.add_argument("--step", type=float, default=4.0, help="calibration nudge, arcmin")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--shoot", help="save a PNG once centred")
    ap.add_argument("--period", type=float, default=20.0)
    add_log_args(ap)
    a = ap.parse_args()
    configure_logging(a)

    lk = Lock(a.host, a.key, a.exp, a.gain)
    try:
        lk.calibrate(a.step)
        while True:
            ok, frac, x = lk.centre_once()
            if ok and a.shoot:
                save_png(x, a.shoot)
                return
            if ok and a.once:
                return
            if a.once and not ok:
                return
            time.sleep(a.period)
    finally:
        lk.close()


if __name__ == "__main__":
    main()
