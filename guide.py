#!/usr/bin/env python3
"""Guiding control for the ASIAIR — on TCP 4400, same channel as the mount.

Guide methods are UNPREFIXED on 4400 (`guide`, `loop`, `get_app_state`,
`set_connected`, `stop_capture`, `get_dither`…) — they'd collide with 4700
names but live on a separate service, so no clash. No auth on 4400.

Typical night-sky flow (each step needs the previous):
    connect on            # set_connected([true])  -> connect the guide cam
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
import sys

from air_rpc import Air

PORT = 4400

# Default dither/settle config — fields lifted from the app's DitherConfig.
DEFAULT_SETTLE = {
    "enable": True, "ra_only": False, "amount": 5,
    "settle_arcsec": 1.5, "settle_time_sec": 10, "settle_timeout_sec": 60,
}


class Guide:
    def __init__(self, host, port=PORT, timeout=10):
        self.air = Air(host, port, timeout=timeout)  # 4400: no auth

    def _r(self, method, params=None):
        reply = self.air.call(method, params or [], timeout=15)
        if isinstance(reply, dict) and reply.get("error"):
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
    def set_connected(self, on):  return self._r("set_connected", [bool(on)])
    def set_exposure(self, ms):   return self._r("set_exposure", [int(ms)])
    def loop(self):               return self._r("loop")
    def stop(self):               return self._r("stop_capture")
    def clear_calibration(self):  return self._r("clear_calibration")

    # --- guiding (calibration SLEWS the mount) ---
    def start(self, settle=None): return self._r("guide", [settle or DEFAULT_SETTLE])

    def close(self):
        self.air.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.2.149")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("state"); sub.add_parser("cameras")
    sub.add_parser("loop"); sub.add_parser("stop"); sub.add_parser("start")
    sub.add_parser("calibrated"); sub.add_parser("history")
    c = sub.add_parser("connect"); c.add_argument("state", choices=["on", "off"])
    e = sub.add_parser("expose"); e.add_argument("ms", type=int)
    a = ap.parse_args()

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
        elif a.cmd == "start":
            print("guide (calibrate+guide) ->", g.start())
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
        print("ERROR:", e, file=sys.stderr); sys.exit(1)
