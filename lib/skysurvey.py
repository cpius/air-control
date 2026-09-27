#!/usr/bin/env python3
"""Horizon survey by plate solve: one (az, alt) at a time, east balcony.

Why plate solve rather than star counting: the solve reports where the frame
TRULY is, independent of the mount's register frame, so the map is anchored to
real sky and survives a re-home. `star_number` and the solve duration come free
in the same result, and a solve that fails IS the "blocked by masonry" signal.

Everything happens on the Air. We never download the frame -- a 7 MB pull costs
9 s and tells us nothing the solver has not already counted.

Hard rules baked in, each learned the expensive way:
  * `start_solve` does NOT expose. Fire start_exposure and wait for the
    Exposure event first, or you silently re-solve the previous field.
  * `get_last_solve_result` returns the PREVIOUS solve with state "complete".
    Reject any result whose image_id did not advance.
  * This solver takes 26-54 s on sky. A 25 s cap reads open sky as blocked.
  * Both 4700 and 4400 drop idle sockets. Poke them.
  * `is_enable_track` switches itself off silently; re-assert every pass.
  * Solve coordinates are JNow (apparent), not J2000.
"""
import argparse, json, math, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from air_rpc import Air
from airlog import get_logger

log = get_logger("skysurvey")
HOST = os.environ.get("ASIAIR_HOST", "192.168.1.35")
KEY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "embedded_key.pem")
LAT, LON = 55.689444, 12.555278
OUT = os.path.expanduser(os.environ.get("SKYSURVEY_OUT", "~/ASICAP/skysurvey-east.jsonl"))
HTML = os.path.expanduser(os.environ.get("SKYSURVEY_HTML", "~/ASICAP/sky-survey.html"))
D, R = math.degrees, math.radians


def altaz_to_radec(az, alt, lst_h):
    az_r, alt_r, lat = R(az), R(alt), R(LAT)
    dec = math.asin(math.sin(alt_r) * math.sin(lat) +
                    math.cos(alt_r) * math.cos(lat) * math.cos(az_r))
    cosha = (math.sin(alt_r) - math.sin(dec) * math.sin(lat)) / (math.cos(dec) * math.cos(lat))
    ha = math.acos(max(-1.0, min(1.0, cosha)))
    if math.sin(az_r) > 0:
        ha = -ha
    return (lst_h - D(ha) / 15.0) % 24, D(dec)


def radec_to_altaz(ra_h, dec_deg, lst_h):
    ha = R((lst_h - ra_h) * 15.0)
    dec, lat = R(dec_deg), R(LAT)
    alt = math.asin(math.sin(dec) * math.sin(lat) +
                    math.cos(dec) * math.cos(lat) * math.cos(ha))
    az = math.atan2(-math.cos(dec) * math.sin(ha),
                    math.sin(dec) * math.cos(lat) - math.cos(dec) * math.sin(lat) * math.cos(ha))
    return D(alt), D(az) % 360


class Rig:
    """Two channels, both self-healing: the Air drops idle sockets on each."""

    def __init__(self):
        self.c = self._new(4700)
        self.m = self._new(4400)

    def _new(self, port):
        return (Air(HOST, 4700, key=KEY, timeout=20) if port == 4700
                else Air(HOST, 4400, timeout=12))

    def _call(self, which, port, method, params=None, t=20):
        for _ in range(4):
            try:
                return getattr(self, which).call(method, params or [], timeout=t)
            except Exception as e:
                log.warn("%s failed (%s) — reconnecting %d", method, type(e).__name__, port)
                try: getattr(self, which).close()
                except Exception: pass
                time.sleep(1.0)
                try: setattr(self, which, self._new(port))
                except Exception: time.sleep(2.0)
        raise RuntimeError(method + " failed after retries")

    def cam(self, method, params=None, t=20):
        return self._call("c", 4700, method, params, t)

    def mnt(self, method, params=None, t=12):
        return self._call("m", 4400, method, params, t)

    def state(self):
        return self.mnt("scope_get_info")["result"]

    def horiz(self):
        r = self.mnt("scope_get_horiz_coord")["result"]
        return r[0], r[1]          # alt, az

    def ensure_tracking(self):
        s = self.state()
        if not s.get("is_enable_track"):
            log.warn("tracking was OFF — re-enabling")
            self.mnt("scope_set_track_state", [True])
            s = self.state()
        return s


def expose(rig, exp_s):
    """One exposure, server-side. No download."""
    rig.cam("set_control_value", ["Exposure", int(exp_s * 1e6)])
    rig.cam("set_control_value", ["Gain", 250])
    rig.c.drain_events()
    rig.cam("start_exposure")
    t0, deadline = time.time(), time.time() + exp_s + 30
    while time.time() < deadline:
        for e in rig.c.drain_events():
            if e.get("Event") == "Exposure":
                if e.get("state") == "complete":
                    return True, time.time() - t0
                if e.get("state") in ("fail", "error"):
                    log.warn("exposure failed: %s", e)
                    return False, time.time() - t0
        time.sleep(0.2)
    return False, time.time() - t0


def plate_solve(rig, cap=75.0):
    """Solve the frame in the buffer. (result, seconds) — result None if no solve."""
    prev = rig.cam("get_last_solve_result").get("result")
    prev_id = prev.get("image_id") if isinstance(prev, dict) else None

    rig.c.drain_events()
    rig.cam("start_solve")
    t0, last_poke, state = time.time(), time.time(), None
    while time.time() - t0 < cap:
        for e in rig.c.drain_events():
            if e.get("Event") == "PlateSolve":
                state = e.get("state")
        if state in ("complete", "fail", "error"):
            break
        if time.time() - last_poke > 4.0:
            try: rig.cam("get_camera_state", t=10)
            except Exception: pass
            try: rig.mnt("scope_get_horiz_coord", t=10)   # 4400 keepalive too
            except Exception: pass
            last_poke = time.time()
        time.sleep(0.25)
    dt = time.time() - t0

    if state != "complete":
        try: rig.cam("stop_solve")
        except Exception: pass
        return None, dt, state
    r = rig.cam("get_last_solve_result").get("result")
    if not isinstance(r, dict) or r.get("state") != "complete":
        return None, dt, "no-result"
    if prev_id is not None and r.get("image_id") == prev_id:
        log.warn("STALE solve (image_id still %s) — discarding", prev_id)
        return None, dt, "stale"
    return r, dt, "complete"


SAFE_ALT = 30.0    # swing azimuth up here, never down among the cables


def goto_altaz(rig, az, alt, timeout=100, two_step=True):
    """Slew to a horizon position. Arrival = position going stable, because
    move_status reads 'none' mid-slew on this firmware.

    Low targets are approached in TWO steps -- swing the azimuth across at
    SAFE_ALT, then descend in place. A single diagonal slew to a low altitude
    drags the OTA sideways at cable height, which on 2026-08-25 snagged a cable,
    stalled the Dec axis and browned out the Air. Cost is a few extra seconds.
    """
    if two_step and alt < 20.0:
        cur_alt, cur_az = rig.horiz()
        d_az = abs((az - cur_az + 180) % 360 - 180)
        if d_az > 15.0:
            log.info("low target + %.0f deg of azimuth — staging via alt %.0f",
                     d_az, SAFE_ALT)
            _slew(rig, az, SAFE_ALT, timeout)
        else:
            log.info("low target but only %.1f deg of azimuth — descending in place", d_az)
    return _slew(rig, az, alt, timeout)


def _slew(rig, az, alt, timeout=100):
    ra, dec = altaz_to_radec(az, alt, rig.state()["sidereal_time"])
    log.info("goto az %.1f alt %.1f  ->  RA %.4fh Dec %+.3f", az, alt, ra, dec)
    r = rig.mnt("scope_goto", [ra, dec], t=20)
    if r.get("code", 0) not in (0, None):
        return None, f"scope_goto refused: {r.get('error')} (code {r.get('code')})"
    t0, prev, stable = time.time(), None, 0
    while time.time() - t0 < timeout:
        time.sleep(1.5)
        cur = rig.horiz()
        if prev and abs(cur[0] - prev[0]) < 0.02 and abs(cur[1] - prev[1]) < 0.02:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        prev = cur
    a, z = rig.horiz()
    return {"alt": a, "az": z}, None


def shot(rig, az, alt, exp=6.0, cap=75.0, label=None, move=True, out=OUT):
    rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "want_az": round(az, 2),
           "want_alt": round(alt, 2), "exp": exp}
    if label:
        rec["label"] = label

    s = rig.ensure_tracking()
    rec["volts"] = round(s.get("input_voltage", 0) / 1000.0, 2)

    if move:
        reg, err = goto_altaz(rig, az, alt)
        if err:
            rec.update(error=err, verdict="ERROR")
            _write(out, rec); return rec
        rec["reg_az"], rec["reg_alt"] = round(reg["az"], 3), round(reg["alt"], 3)
        if abs(reg["alt"] - alt) > 3.0:
            rec["note"] = f"landed {reg['alt']-alt:+.1f}deg off in altitude"

    ok, t_exp = expose(rig, exp)
    if not ok:
        log.warn("exposure failed — retrying once (usually a dropped socket)")
        time.sleep(2.0)
        ok, t_exp = expose(rig, exp)
    rec["exp_wall_s"] = round(t_exp, 1)
    if not ok:
        rec.update(error="exposure failed twice", verdict="ERROR")
        _write(out, rec); return rec

    lst = rig.state()["sidereal_time"]
    r, dt, state = plate_solve(rig, cap=cap)
    rec["solve_s"] = round(dt, 1)
    rec["solve_state"] = state
    if r is None:
        # HOW the solve failed matters more than that it failed.
        #   fast "fail"  = the solver saw too few stars to try   -> masonry
        #   long "solving" grind = plenty of stars, no match     -> NOT masonry
        # Verified 2026-08-25: every genuine roofline fast-failed in ~3 s, while
        # az 210 ground for 75 s on frames holding 30-300 sharp stars.
        if state == "fail" and dt < 20.0:
            rec.update(solved=False, stars=0, verdict="BLOCKED")
        else:
            rec.update(solved=False, stars=0, verdict="AMBIGUOUS",
                       note=f"solver ground {dt:.0f}s in state '{state}' — stars "
                            f"present but unmatched; NOT evidence of masonry")
    else:
        ra_h, dec_d = r["ra_dec"]
        t_alt, t_az = radec_to_altaz(ra_h, dec_d, lst)
        rec.update(solved=True, stars=r.get("star_number"),
                   ra_h=round(ra_h, 5), dec_deg=round(dec_d, 5),
                   true_az=round(t_az, 3), true_alt=round(t_alt, 3),
                   angle=round(r.get("angle", 0), 2),
                   image_id=r.get("image_id"), verdict="OPEN")
        if "reg_az" in rec:
            rec["off_az"] = round((t_az - rec["reg_az"] + 180) % 360 - 180, 3)
            rec["off_alt"] = round(t_alt - rec["reg_alt"], 3)
    _write(out, rec)
    return rec


def _write(out, rec):
    with open(out, "a") as f:
        f.write(json.dumps(rec) + "\n")


def fmt(r):
    if r.get("verdict") == "ERROR":
        return f"az {r['want_az']:6.1f} alt {r['want_alt']:5.1f}   ERROR: {r.get('error')}"
    if not r.get("solved"):
        return (f"az {r['want_az']:6.1f} alt {r['want_alt']:5.1f}   {r.get('verdict','BLOCKED'):9s}  "
                f"(no solve, gave up at {r['solve_s']:.0f}s, {r['exp']:.0f}s exp)")
    return (f"az {r['want_az']:6.1f} alt {r['want_alt']:5.1f}   OPEN  "
            f"true az {r['true_az']:6.2f} alt {r['true_alt']:5.2f}  "
            f"{r['stars']:4d} stars  solve {r['solve_s']:5.1f}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("az", type=float)
    ap.add_argument("alt", type=float)
    ap.add_argument("--exp", type=float, default=6.0)
    ap.add_argument("--cap", type=float, default=75.0)
    ap.add_argument("--label", default=None)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--no-move", action="store_true")
    a = ap.parse_args()

    rig = Rig()
    r = shot(rig, a.az, a.alt, a.exp, a.cap, a.label, move=not a.no_move, out=a.out)
    print("\n" + fmt(r))
    print(json.dumps(r))


if __name__ == "__main__":
    main()
