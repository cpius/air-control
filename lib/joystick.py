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
from airlog import add_log_args, configure_logging, get_logger

log = get_logger("joystick")

HOST = os.environ.get("ASIAIR_HOST")


class Joystick:
    def __init__(self, host=HOST, require_mount=True):
        self.host = host
        self.air = Air(host, 4400, timeout=10)
        if require_mount and not self.mount_attached():
            raise RuntimeError(
                "the Air has no mount attached, so scope_move would do nothing.\n"
                "  fix:  python3 mount.py connect --lat <lat> --lon <lon>\n"
                "(an Air restart drops the mount, and attaching zeroes the site)")

    def mount_attached(self):
        r = self.air.call("get_connected", [], timeout=10)
        res = r.get("result") if isinstance(r, dict) else None
        return isinstance(res, dict) and "mount" in res

    def _r(self, m, p=None, t=12, tries=3):
        """Call on 4400, reconnecting if the Air drops the socket.

        A long-lived 4400 connection will eventually give BrokenPipe or a read
        timeout. An RPC-level error is a real answer, not a transport failure,
        so it is re-raised immediately rather than retried.
        """
        for i in range(tries):
            try:
                r = self.air.call(m, p or [], timeout=t)
                if isinstance(r, dict) and r.get("error"):
                    raise RuntimeError(f"{m}: {r['error']} (code {r.get('code')})")
                return r.get("result")
            except RuntimeError:
                raise
            except Exception as e:
                if i == tries - 1:
                    log.error("%s failed %d times on 4400: %s", m, tries, e)
                    raise
                log.warn("%s failed (%s) — reconnecting 4400, attempt %d/%d",
                         m, e, i + 2, tries)
                try:
                    self.air.close()
                except Exception:
                    pass
                time.sleep(1.0)
                self.air = Air(self.host, 4400, timeout=10)

    def state(self):
        return self._r("scope_get_info")

    def rates(self):
        st = self.state()
        return st["slew_rate_list"], st["slew_rate_index"]

    def set_rate(self, idx):
        return self._r("scope_set_slew_rate", [int(idx)])

    def move(self, direction, speed=None):
        p = [direction] if speed is None else [direction, int(speed)]
        log.debug("scope_move %s", p)
        return self._r("scope_move", p)

    def stop(self):
        try:
            return self._r("scope_move", ["none"])
        except Exception as e:
            log.warn("stop failed (%s) — THE MOUNT MAY STILL BE MOVING", e)
            return None

    def nudge(self, direction, seconds, speed=None):
        """Move in a direction for a fixed time, then stop. Returns (dRA_deg, dDec_deg).

        The mount is physically moving for the whole of `seconds`, which in a
        spiral search is routinely 10-30 s per step. Nothing else reports that,
        so the countdown here is the only sign the rig is not simply wedged.
        """
        a = self.state()
        t0 = time.time()
        log.info("nudge %s for %.2fs from RA=%.4f Dec=%.4f",
                 direction, seconds, a["RA"], a["Dec"])
        self.move(direction, speed)
        try:
            with log.slow("moving %s" % direction, quiet_for=1.0,
                          detail=lambda: "%.1fs of %.1fs (%.0f%%)"
                                         % (time.time() - t0, seconds,
                                            100.0 * min(1.0, (time.time() - t0) / seconds))):
                while time.time() - t0 < seconds:
                    time.sleep(0.02)
        finally:
            self.stop()
        with log.slow("settling", quiet_for=1.0):
            time.sleep(0.6)                  # let it settle
        b = self.state()
        dra = (b["RA"] - a["RA"]) * 15.0 * math.cos(math.radians((a["Dec"] + b["Dec"]) / 2))
        ddec = b["Dec"] - a["Dec"]
        log.info("  moved dRA=%+.4f deg dDec=%+.4f deg in %.2fs (%.2f arcmin/s)",
                 dra, ddec, seconds, math.hypot(dra, ddec) * 60 / max(seconds, 1e-6))
        return dra, ddec, a, b

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
    add_log_args(ap)
    args = ap.parse_args()
    configure_logging(args)

    j = Joystick(args.host)
    try:
        st = j.state()
        names, idx = j.rates()
        print(f"mount at RA={st['RA']:.4f} Dec={st['Dec']:.4f} alt={st['Alt']:.1f}")
        print(f"slew_rate_list = {names}   current index = {idx} ({names[idx]})")
        print("\ncalibrating: 'south' for 2.0 s at each rate\n")
        results = {}
        for i in (0, 1, 2, 3, 4):
            log.info("--- calibrating rate %d (%s) ---", i, names[i])
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
