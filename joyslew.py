#!/usr/bin/env python3
"""Slew with the joystick, closed-loop on the mount register, for when goto is
dead (2026-08-27, 2026-09-03: `scope_goto` returns 0 in 57 ms with an empty
route and nothing moves, while `scope_move` works and the register follows it).

Safety rules learned the hard way tonight:
  * 4400 drops an idle socket at ~15 s. A pulse longer than that loses its
    stop and the mount runs away (it ran ~30 s / 3.6 deg once). Pulses are
    chunked at <= 4 s with a register read in between.
  * A script killed mid-pulse leaves the joystick running. Every exit path
    sends scope_move ["none"]; there is also a hard wall-clock cap.
  * The 20x rate moves ~5'/s for 2 s pulses but ~10'/s for long ones (ramp),
    so the rate is re-estimated from every chunk instead of assumed.
  * Sky coordinates go in as explicit numbers; never trust a column index.

    python3 joyslew.py --ra 3.2637 --dec 22.89          # JNow/apparent, hours & degrees
    python3 joyslew.py --ra 3.2637 --dec 22.89 --tol 1.0 --max-minutes 6
"""
import argparse
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from air_rpc import Air

CHUNK = 4.0


class Joy:
    def __init__(self, host):
        self.host = host
        self.m = Air(host, 4400, timeout=10)

    def c(self, meth, p=None):
        for i in range(3):
            try:
                return self.m.call(meth, p or [], timeout=15)
            except Exception:
                try:
                    self.m.close()
                except Exception:
                    pass
                time.sleep(0.5)
                self.m = Air(self.host, 4400, timeout=10)
        raise RuntimeError(meth)

    def state(self):
        return self.c("scope_get_info")["result"]

    def stop(self):
        try:
            self.c("scope_move", ["none"])
        except Exception:
            pass

    def chunk(self, direction, seconds):
        """One pulse of <= CHUNK seconds. Returns (dRA_deg, dDec_deg) it produced."""
        seconds = min(CHUNK, max(0.2, seconds))
        s0 = self.state()
        self.c("scope_move", [direction])
        time.sleep(seconds)
        self.c("scope_move", ["none"])
        time.sleep(0.6)
        s1 = self.state()
        return (s1["RA"] - s0["RA"]) * 15 * math.cos(math.radians(s1["Dec"])), s1["Dec"] - s0["Dec"], seconds


def slew(joy, ra_h, dec_d, tol_arcmin=1.5, max_minutes=8.0, rate_index=4, log=print):
    """Return the final state, or raise. Closed loop on the register."""
    t_end = time.time() + max_minutes * 60
    saved = joy.state().get("slew_rate_index")
    joy.c("scope_set_slew_rate", [int(rate_index)])
    # deg/s estimates, refined from every chunk; start conservative
    rate = {"ra": 5.0 / 60, "dec": 5.0 / 60}
    sign = {"ra": None, "dec": None}     # measured: does "east" raise RA? does "south" raise Dec?
    try:
        it = 0
        while time.time() < t_end:
            st = joy.state()
            dra = (ra_h - st["RA"]) * 15 * math.cos(math.radians(st["Dec"]))
            ddec = dec_d - st["Dec"]
            log("iter %d: RA %.4fh Dec %+.4f  alt %.1f  need %+.1f' RA %+.1f' Dec" % (
                it, st["RA"], st["Dec"], st["Alt"], dra * 60, ddec * 60))
            if abs(dra) * 60 < tol_arcmin and abs(ddec) * 60 < tol_arcmin:
                return st
            if st["Alt"] < 5:
                raise RuntimeError("altitude %.1f -- refusing to continue" % st["Alt"])
            for axis, err, pos_dir, neg_dir in (("ra", dra, "east", "west"), ("dec", ddec, "south", "north")):
                if abs(err) * 60 < tol_arcmin:
                    continue
                want = pos_dir if err > 0 else neg_dir
                if sign[axis] is False:          # measured reversed on this pier side
                    want = neg_dir if err > 0 else pos_dir
                # Overshoot killed the first Hamal run (2026-09-03, 60x): a rate
                # learned from short ramp-limited chunks underestimates the
                # sustained speed, so a long pulse overshoots by 2-3x. Learn the
                # rate as the MAX seen and never command more than 60% of the error.
                secs = abs(err) / rate[axis] * 0.6
                mra, mdec, secs = joy.chunk(want, secs)
                moved = mra if axis == "ra" else mdec
                # Register jump detector (2026-09-03): in the dead-goto state the
                # AM5N's reported position jumped ~3 deg mid-pulse three times in a
                # night. A 20x chunk can move at most ~1 deg; a 60x chunk ~3 deg.
                other = mdec if axis == "ra" else mra
                cap = 0.25 * secs * (1.0 if rate_index <= 4 else 4.0) + 0.2   # deg
                if abs(moved) > cap or abs(other) > 0.3:
                    raise RuntimeError("register jump: %s pulse of %.1fs 'moved' %+.2f deg (other axis %+.2f) -- "
                                       "mount bookkeeping is wedged, power-cycle it and solve" % (axis, secs, moved, other))
                if abs(moved) * 60 > 0.3:
                    rate[axis] = max(rate[axis], abs(moved) / secs)
                    went_positive = moved > 0
                    commanded_positive = (want == pos_dir)
                    sign[axis] = (went_positive == commanded_positive) if sign[axis] is None else sign[axis]
                    if went_positive != (err > 0):
                        # moved the wrong way: flip our idea of this axis' sign
                        sign[axis] = not (sign[axis] if sign[axis] is not None else True)
                        log("   %s moved the wrong way (%+.1f') -- flipping direction sense" % (axis, moved * 60))
            it += 1
        raise RuntimeError("wall-clock cap reached")
    finally:
        joy.stop()
        if saved is not None:
            try:
                joy.c("scope_set_slew_rate", [int(saved)])
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--ra", type=float, required=True, help="hours, apparent/JNow")
    ap.add_argument("--dec", type=float, required=True, help="degrees")
    ap.add_argument("--tol", type=float, default=1.5, help="arcmin")
    ap.add_argument("--max-minutes", type=float, default=8.0)
    ap.add_argument("--rate", type=int, default=4, help="slew_rate_list index (4 = 20x)")
    a = ap.parse_args()
    joy = Joy(a.host)
    try:
        st = slew(joy, a.ra, a.dec, a.tol, a.max_minutes, a.rate)
        print("arrived: RA %.4fh Dec %+.4f alt %.1f az %.1f" % (st["RA"], st["Dec"], st["Alt"], st["Az"]))
    finally:
        joy.stop()
        joy.m.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
