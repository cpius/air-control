#!/usr/bin/env python3
"""Joystick (directional) mount control on 4400 + rate calibration.

scope_move takes [dir] or [dir, speed]; dir is "north"/"south"/"east"/"west",
and "none" stops. scope_set_slew_rate takes an index into slew_rate_list.
Directional moves are RELATIVE, so they work even when the pointing model is
garbage and plate solving is unavailable (e.g. before focus is achieved).

Always stops the motion in a finally: an un-stopped joystick keeps slewing.
"""
import math, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from air_rpc import Air

HOST = os.environ.get("ASIAIR_HOST")


class Joystick:
    def __init__(self, host=HOST):
        self.air = Air(host, 4400, timeout=10)

    def _r(self, m, p=None, t=12):
        r = self.air.call(m, p or [], timeout=t)
        if isinstance(r, dict) and r.get("error"):
            raise RuntimeError(f"{m}: {r['error']} (code {r.get('code')})")
        return r.get("result")

    def state(self):
        return self._r("scope_get_info")

    def rates(self):
        st = self.state()
        return st["slew_rate_list"], st["slew_rate_index"]

    def set_rate(self, idx):
        return self._r("scope_set_slew_rate", [int(idx)])

    def move(self, direction, speed=None):
        p = [direction] if speed is None else [direction, int(speed)]
        return self._r("scope_move", p)

    def stop(self):
        try:
            return self._r("scope_move", ["none"])
        except Exception:
            return None

    def nudge(self, direction, seconds, speed=None):
        """Move in a direction for a fixed time, then stop. Returns (dRA_deg, dDec_deg)."""
        a = self.state()
        t0 = time.time()
        self.move(direction, speed)
        try:
            while time.time() - t0 < seconds:
                time.sleep(0.02)
        finally:
            self.stop()
        time.sleep(0.6)                      # let it settle
        b = self.state()
        dra = (b["RA"] - a["RA"]) * 15.0 * math.cos(math.radians((a["Dec"] + b["Dec"]) / 2))
        return dra, b["Dec"] - a["Dec"], a, b

    def close(self):
        try:
            self.stop()
        finally:
            self.air.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Directional mount control + rate calibration")
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    args = ap.parse_args()

    j = Joystick(args.host)
    try:
        st = j.state()
        names, idx = j.rates()
        print(f"mount at RA={st['RA']:.4f} Dec={st['Dec']:.4f} alt={st['Alt']:.1f}")
        print(f"slew_rate_list = {names}   current index = {idx} ({names[idx]})")
        print("\ncalibrating: 'south' for 2.0 s at each rate\n")
        results = {}
        for i in (0, 1, 2, 3, 4):
            j.set_rate(i)
            time.sleep(0.4)
            dra, ddec, a, b = j.nudge("south", 2.0)
            rate_deg_s = abs(ddec) / 2.0
            results[i] = rate_deg_s
            print(f"  rate[{i}]={names[i]:>6}: dDec={ddec:+8.4f} deg  dRA={dra:+7.4f} deg"
                  f"  -> {rate_deg_s*3600:8.1f} arcsec/s ({rate_deg_s*60:6.2f} arcmin/s)")
        print("\nfield of view is 30.1 x 16.9 arcmin at 1271 mm, so one field-step is:")
        for i, r in results.items():
            if r > 0:
                print(f"  rate[{i}]={names[i]:>6}: {16.9/(r*60):6.2f} s per 16.9' (short axis)")
    finally:
        j.close()
