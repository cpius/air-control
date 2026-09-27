#!/usr/bin/env python3
"""Survey the local skyline: what altitude does the building line reach, per azimuth.

The rig lives on a balcony in a city block, so most of the sky is wall. Knowing
*where* the roofline sits turns "the target never showed up" into "the target is
behind the building" -- a distinction that cost a whole Moon session.

Method: park the mount at a grid of (az, alt), take a short exposure, and
classify sky against masonry. Under city cloud the sky is a bright, smooth,
vertically-graded field; a building is darker and, crucially, *structured* --
edges, gutters, lit windows -- so the spatial standard deviation separates them
where the mean alone does not.

Two things make this awkward and are handled here:

  * The mount is equatorial, so there is no alt/az slew. `to_altaz` closes the
    loop on the mount's own reported Alt/Az with a locally-measured Jacobian.
  * Tracking must be OFF. With the axes stationary the physical alt/az is fixed,
    which is exactly what a horizon survey wants; with tracking on, every
    measurement slides at 15 arcmin/min.

SAFETY: `ALT_FLOOR` is a hard stop. Twice on 2026-08-23 a drive put the tube
below the horizon, once to Alt -59. Every move is verified and anything that
would go under the floor is refused.

    python3 horizon/horizon.py probe --az 190          # one vertical profile
    python3 horizon/horizon.py survey --out horizon.json
"""
import argparse, json, math, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import numpy as np
from air_rpc import Air
from starhunt import Camera
from airlog import add_log_args, configure_logging, get_logger

log = get_logger("horizon")
KEY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "embedded_key.pem")

ALT_FLOOR = -1.0        # never point below this, ever
RATE_INDEX = 4          # 20x
ARCMIN_PER_S = 5.46


class Rig:
    def __init__(self, host, key, exp, gain):
        self.m = Air(host, 4400, timeout=8)
        self.m.call("scope_set_slew_rate", [RATE_INDEX], timeout=8)
        self.tracking_off()
        self.cam = Camera(host, key, cam_name="ZWO ASI585MC Air")
        self.exp, self.gain = exp, gain

    def st(self):
        for i in range(4):
            try:
                return self.m.call("scope_get_info", [], timeout=8)["result"]
            except Exception:
                time.sleep(1.0)
        raise RuntimeError("mount unreachable")

    def tracking_off(self):
        """A horizon survey wants the axes still -- then alt/az is constant."""
        self.m.call("scope_set_track_state", [False], timeout=8)

    def stop(self):
        self.m.call("scope_move", ["none"], timeout=8)

    def nudge(self, d, arcmin):
        """Timed move at 20x, refusing anything that would breach the floor."""
        before = self.st()
        t = max(0.15, min(12.0, abs(arcmin) / ARCMIN_PER_S))
        self.m.call("scope_move", [d], timeout=8)
        t0 = time.time()
        while time.time() - t0 < t:
            time.sleep(0.05)
            if time.time() - t0 > 1.0:                 # watch altitude mid-move
                try:
                    if self.st()["Alt"] < ALT_FLOOR:
                        self.stop()
                        log.error("ALT FLOOR breached mid-move -- stopped")
                        break
                except Exception:
                    pass
        self.stop()
        time.sleep(0.6)
        after = self.st()
        if after["Alt"] < ALT_FLOOR:
            log.error("below floor (Alt=%.2f) -- backing off", after["Alt"])
            self.m.call("scope_move", ["north"], timeout=8)
            time.sleep(1.5)
            self.stop()
        return before, after

    def jacobian(self, step=20.0):
        """d(Alt,Az)/d(north,east), measured right here -- it varies over the sky."""
        a0 = self.st()
        _, a1 = self.nudge("north", step)
        self.nudge("south", step)
        a2 = self.st()
        _, a3 = self.nudge("east", step)
        self.nudge("west", step)
        d = step / 60.0
        def daz(x, y):
            v = x - y
            return (v + 180) % 360 - 180
        J = np.array([[(a1["Alt"] - a0["Alt"]) / d, (a3["Alt"] - a2["Alt"]) / d],
                      [daz(a1["Az"], a0["Az"]) / d, daz(a3["Az"], a2["Az"]) / d]])
        return J

    def to_altaz(self, az, alt, tol=0.6, maxiter=8):
        """Closed-loop onto a reported Alt/Az. Returns the state actually reached."""
        if alt < ALT_FLOOR:
            raise ValueError(f"refusing alt {alt} below floor {ALT_FLOOR}")
        for k in range(maxiter):
            s = self.st()
            dalt = alt - s["Alt"]
            daz = (az - s["Az"] + 180) % 360 - 180
            if abs(dalt) < tol and abs(daz) < tol:
                return s
            J = self.jacobian() if k == 0 else J
            try:
                mv = np.linalg.solve(J, np.array([dalt, daz]))
            except np.linalg.LinAlgError:
                return s
            mv = np.clip(mv, -8, 8)                    # degrees per iteration
            for amt, pos, neg in ((mv[0], "north", "south"), (mv[1], "east", "west")):
                if abs(amt) > 0.05:
                    if pos == "north" or True:
                        self.nudge(pos if amt > 0 else neg, abs(amt) * 60.0)
        return self.st()

    def measure(self):
        """Return (mean, std, vertical gradient) for the current pointing."""
        v, w, h, hdr = self.cam.grab(self.exp, self.gain)
        x = np.frombuffer(v.tobytes(), dtype="<u2").astype(np.float32).reshape(h, w)
        b = x[::4, ::4]
        return float(b.mean()), float(b.std()), float(b[:h//8].mean() - b[-h//8:].mean())

    def close(self):
        try:
            self.stop()
        finally:
            self.cam.close()
            self.m.close()
