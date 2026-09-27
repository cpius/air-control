#!/usr/bin/env python3
"""Guiding control for the ASIAIR — on TCP 4400, same channel as the mount.

Guide methods are UNPREFIXED on 4400 (`guide`, `loop`, `get_app_state`,
`set_connected`, `stop_capture`, `get_dither`…) — they'd collide with 4700
names but live on a separate service, so no clash. No auth on 4400.

Typical night-sky flow (each step needs the previous):
    connect on            # set_connected([{"camera": true}]) -> connect guide cam
    expose 1000           # set_exposure([ms])
    loop                  # start looping (state -> Looping)
    start                 # guide([settle]) -> Calibrating -> Guiding (SLEWS to calibrate)
    stop                  # stop_capture

State (get_app_state): Idle / Looping / Selected / Calibrating / Guiding /
Paused / LostLock / Stopped.

    python3 guide.py state
    python3 guide.py dither         # show dither config
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from air_rpc import Air
from airlog import add_log_args, configure_logging, get_logger

log = get_logger("guide")

PORT = 4400

# `guide` wants a PHD2-style settle object — pixels/time/timeout — followed by a
# recalibrate bool. Verified on sky 2026-08-10; the trailing bool was confirmed
# from the app's own traffic 2026-08-16.
#
# The app itself sends the looser {"pixels": 3, "time": 5, "timeout": 60}. Kept
# tighter here deliberately: on a fainter guide star the loose form settles
# sooner but starts the sub with the star still drifting.
DEFAULT_SETTLE = {"pixels": 1.5, "time": 10, "timeout": 60}

# NOT what `guide` takes: this is the app's DitherConfig, kept for reference
# because it is easy to reach for by mistake. Passing it to `guide` returns
# 102 "invalid params".
DITHER_CONFIG = {
    "enable": True, "ra_only": False, "amount": 5,
    "settle_arcsec": 1.5, "settle_time_sec": 10, "settle_timeout_sec": 60,
}


class Guide:
    def __init__(self, host, port=PORT, timeout=10):
        self.air = Air(host, port, timeout=timeout)  # 4400: no auth

    def _r(self, method, params=None):
        reply = self.air.call(method, params or [], timeout=15)
        if isinstance(reply, dict) and reply.get("error"):
            log.error("%s -> %s (code %s)", method, reply["error"], reply.get("code"))
            raise RuntimeError(f"{method}: {reply['error']} (code {reply.get('code')})")
        return reply.get("result") if isinstance(reply, dict) else reply

    # --- reads (safe) ---
    def state(self):        return self._r("get_app_state")
    def connected(self):    return self._r("get_connected")
    def cameras(self):      return self._r("get_connected_cameras")
    def camera_info(self):  return self._r("get_camera_info")
    def calibrated(self):   return self._r("get_calibrated")
    def get_setting(self):  return self._r("get_setting")
    def get_exposure(self): return self._r("get_exposure")
    def history(self):      return self._r("get_ra_dec_history")   # guide graph

    # dither get/set live on the 4700 main channel, not here:
    #   air_rpc.py --key call get_dither / set_dither
    # (dither pauses the main imaging camera, so the Air keeps it on 4700.)

    # --- setup (safe: no mount motion) ---
    # set_connected takes a Camera bean, NOT a bare bool: [{"camera": true}].
    # The same call connects the MOUNT with a different field — that is the
    # only way back after an Air restart without reaching for the phone.
    def set_connected(self, on):
        return self._r("set_connected", [{"camera": bool(on)}])

    def set_mount_connected(self, on=True):
        return self._r("set_connected", [{"mount": bool(on)}])

    def set_camera_idx(self, idx):
        return self._r("set_camera_idx", [int(idx)])
    def set_exposure(self, ms):   return self._r("set_exposure", [int(ms)])
    def loop(self):               return self._r("loop")
    def stop(self):               return self._r("stop_capture")
    def clear_calibration(self):  return self._r("clear_calibration")

    def set_lock_position(self, x, y, exact=False):
        """Pick the guide star, in the guide service's own coordinate space.

        That space is `get_camera_info.full_size` — **not** sensor pixels. On the
        ASI220MM it reports [960, 540] because the guide stream is binned 2x2, so
        a star found at (712, 435) on the 1920x1080 sensor is set as (356, 218).

        Three params: [x, y, exact]. The trailing bool is easy to miss, and
        omitting it is worse than an error — the two-param form is accepted and
        then silently reverted a few seconds later, so the lock appears to take
        and does not. Verified against the app's traffic 2026-08-16.

        The Air snaps the value to the actual star centroid, so reading it back
        returns something near, not equal to, what you set. `305 could not set
        lock position` means there is no star at those coordinates.

        Needed because guide-star **auto-selection does not work over RPC** —
        `loop` alone leaves the state at `Looping` indefinitely, however rich the
        field. Only the app auto-selects; from here you must pick the star.
        """
        log.info("set_lock_position(%.1f, %.1f, exact=%s) — guide-space coords, "
                 "not sensor pixels", x, y, exact)
        r = self._r("set_lock_position", [float(x), float(y), bool(exact)])
        try:
            log.info("  Air snapped the lock to %s", self._r("get_lock_position"))
        except RuntimeError as e:
            log.warn("  could not read the lock back: %s", e)
        return r

    # --- guiding (calibration SLEWS the mount) ---
    def start(self, settle=None, recalibrate=False):
        """Settle spec plus a trailing bool: [settle, recalibrate].

        The repo previously sent a one-element list. The app sends two — verified
        on the wire 2026-08-16 — and the second controls whether calibration is
        redone rather than reused. Passing False reuses an existing calibration,
        which turns a ~7 minute start into ~10 seconds.
        """
        settle = settle or DEFAULT_SETTLE
        # Calibration SLEWS the mount and takes ~7 minutes; reusing one is ~10 s.
        # The RPC returns on acceptance, so the state poll below is what actually
        # reports progress -- once a second, because a stuck calibration and a
        # working one are otherwise identical for seven minutes.
        log.info("guide start: settle=%s recalibrate=%s (%s)", settle, recalibrate,
                 "expect ~7 min — the mount will slew" if recalibrate
                 else "reusing the stored calibration, expect ~10 s")
        r = self._r("guide", [settle, bool(recalibrate)])
        log.info("guide accepted -> %s; now in state %s", r, self.state())
        return r

    def wait_until(self, states=("Guiding",), timeout=600.0, poll=1.0):
        """Poll get_app_state until it lands in `states`. Returns the state.

        Nothing pushes guiding progress as events, so this is a poll -- and the
        thing being waited on (calibration) is the longest operation in the
        toolkit at ~7 minutes. Every second, say which state it is in.
        """
        t0 = time.time()
        cur = [None]
        with log.slow("waiting for %s" % "/".join(states), quiet_for=1.0,
                      detail=lambda: "state=%s" % cur[0]):
            while time.time() - t0 < timeout:
                st = self.state()
                if st != cur[0]:
                    log.info("guide state: %s -> %s (%.0fs in)", cur[0], st,
                             time.time() - t0)
                    cur[0] = st
                if st in states:
                    return st
                if st in ("Idle", "Stopped", "LostLock"):
                    log.warn("guiding fell back to %s after %.0fs", st,
                             time.time() - t0)
                time.sleep(poll)
        log.error("guiding never reached %s within %.0fs (last state %s)",
                  states, timeout, cur[0])
        raise TimeoutError(f"guide state {cur[0]!r}, wanted one of {states}")

    def close(self):
        self.air.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("state"); sub.add_parser("cameras")
    sub.add_parser("loop"); sub.add_parser("stop")
    st = sub.add_parser("start")
    st.add_argument("--recalibrate", action="store_true",
                    help="redo calibration instead of reusing it (~7 min vs ~10 s)")
    st.add_argument("--wait", type=float, default=0.0, metavar="SECONDS",
                    help="poll until the state reaches Guiding, logging it "
                         "every second (0 = return as soon as it is accepted)")
    lk = sub.add_parser("lock", help="select the guide star (guide-space coords)")
    lk.add_argument("x", type=float); lk.add_argument("y", type=float)
    sub.add_parser("calibrated"); sub.add_parser("history")
    c = sub.add_parser("connect"); c.add_argument("state", choices=["on", "off"])
    e = sub.add_parser("expose"); e.add_argument("ms", type=int)
    add_log_args(ap)
    a = ap.parse_args()
    configure_logging(a)
    log.info("guide %s -> %s:%d", a.cmd, a.host, PORT)

    g = Guide(a.host)
    try:
        if a.cmd == "state":
            print("guide state:", g.state(), "| connected:", g.connected(),
                  "| calibrated:", g.calibrated())
        elif a.cmd == "cameras":
            print(json.dumps(g.cameras(), indent=2, ensure_ascii=False))
        elif a.cmd == "connect":
            print("set_connected ->", g.set_connected(a.state == "on"))
        elif a.cmd == "expose":
            print("set_exposure ->", g.set_exposure(a.ms))
        elif a.cmd == "loop":
            print("loop ->", g.loop())
        elif a.cmd == "lock":
            print(f"set_lock_position({a.x}, {a.y}) ->", g.set_lock_position(a.x, a.y))
            print("lock now:", g._r("get_lock_position"), "| state:", g.state())
        elif a.cmd == "start":
            print("guide ->", g.start(recalibrate=a.recalibrate))
            if a.wait:
                print("state ->", g.wait_until(timeout=a.wait))
        elif a.cmd == "stop":
            print("stop_capture ->", g.stop())
        elif a.cmd == "calibrated":
            print("calibrated:", g.calibrated())
        elif a.cmd == "history":
            print(json.dumps(g.history(), ensure_ascii=False)[:500])
    finally:
        g.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        log.error("%s", e)
        print("ERROR:", e, file=sys.stderr); sys.exit(1)
