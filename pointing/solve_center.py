#!/usr/bin/env python3
"""Plate-solve-and-center ("goto accuracy") via the ASIAIR's native routine.

This is `start_auto_goto` on the 4700 main channel (needs the RSA key): the Air
takes an exposure, plate-solves, computes the pointing error, nudges the mount
(over 4400 internally), and repeats until centered. Params come from the app's
MainCameraGateway.startAutoGoto(ra, dec, angle):

    start_auto_goto -> [ra_hours, dec_deg, angle_or_null]     (on 4700)

Progress arrives as events; we stream them until a terminal one. A blind goto
happens first, so this WILL slew — we refuse targets below the horizon using the
mount's live latitude + sidereal time (read from 4400).

    python3 solve_center.py 20.016 35.365            # RA hours, Dec deg
    python3 solve_center.py 20.016 35.365 --angle 0  # with rotator angle
"""

import argparse
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from air_rpc import Air
from airlog import add_log_args, configure_logging, get_logger
from mount import Mount

log = get_logger("solve")

MAIN_PORT = 4700


def altitude(ra_h, dec_deg, lat_deg, lst_h):
    """Approx altitude (deg) of RA/Dec at local sidereal time — horizon guard."""
    ha = math.radians((lst_h - ra_h) * 15.0)
    dec, lat = math.radians(dec_deg), math.radians(lat_deg)
    sin_alt = math.sin(dec) * math.sin(lat) + math.cos(dec) * math.cos(lat) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_alt))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ra", type=float, help="target RA in hours")
    ap.add_argument("dec", type=float, help="target Dec in degrees")
    ap.add_argument("--angle", type=float, default=None, help="rotator angle (optional)")
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--min-alt", type=float, default=10.0, help="refuse below this altitude")
    ap.add_argument("--seconds", type=int, default=180, help="max time to watch events")
    ap.add_argument("--solve-timeout", type=float, default=20.0,
                    help="abort if a single plate solve runs longer than this (0 = no cap)")
    ap.add_argument("--max-step-fails", type=int, default=2,
                    help="give up after this many failed auto-goto steps")
    ap.add_argument("--force", action="store_true", help="skip the horizon check")
    add_log_args(ap)
    a = ap.parse_args()
    configure_logging(a)
    log.info("solve_center RA=%.4fh Dec=%.4f angle=%s (solve cap %.0fs, "
             "%d step failure(s) allowed)", a.ra, a.dec, a.angle,
             a.solve_timeout, a.max_step_fails)

    # Horizon safety, from the mount's own location + sidereal time (4400).
    mnt = Mount(a.host)
    try:
        st = mnt.state()
        lat, lst = st["Lat"], st["sidereal_time"]
    finally:
        mnt.close()
    alt = altitude(a.ra, a.dec, lat, lst)
    print(f"target RA={a.ra}h Dec={a.dec}°  ->  altitude ≈ {alt:.1f}° "
          f"(lat {lat:.3f}, LST {lst:.3f}h)")
    log.info("altitude %.1f deg (lat %.3f, LST %.3fh)", alt, lat, lst)
    if alt < a.min_alt and not a.force:
        log.error("target is %.1f deg up, below the %.1f deg floor — refusing",
                  alt, a.min_alt)
        print(f"REFUSING: target is below {a.min_alt}° altitude. Use --force to override.",
              file=sys.stderr)
        return 2

    air = Air(a.host, MAIN_PORT, key="embedded_key.pem")
    if not air.verified:
        print("4700 auth failed", file=sys.stderr); return 1

    # Main camera must be open for the Air to expose+solve. A BARE open_camera
    # opens the GUIDE sensor, which would solve on the wrong chip (a ~21'x12'
    # field offset half a degree from the imaging field) — so name it. The
    # main camera is the one with the larger chip.
    cams = air.call("get_connected_cameras", [], timeout=10).get("result") or []
    main = max(cams, key=lambda c: c.get("chip_size", [0, 0])[0], default=None)
    if not main:
        print("no cameras reported by the Air", file=sys.stderr)
        return 1
    with log.slow("opening the main camera %s" % main["name"]):
        air.call("open_camera", [main["name"]], timeout=25)
    print(f"main camera: {main['name']}", flush=True)

    params = [a.ra, a.dec, a.angle]   # matches startAutoGoto(ra, dec, angle)
    print("start_auto_goto ->",
          air.call("start_auto_goto", params, timeout=15).get("result"), flush=True)

    print(f"\nwatching events up to {a.seconds}s (Ctrl-C to stop watching; the "
          f"routine keeps running on the Air):")
    t0 = time.time()
    end = t0 + a.seconds
    solve_started = None      # when the in-flight PlateSolve began
    step_fails = 0
    seen = [0]
    last = [t0, "none yet"]

    def watching():
        bits = ["%d event(s), last %r %.0fs ago"
                % (seen[0], last[1], time.time() - last[0])]
        if solve_started:
            bits.append("solving for %.0fs of %.0fs allowed"
                        % (time.time() - solve_started, a.solve_timeout))
        if step_fails:
            bits.append("%d/%d step failure(s)" % (step_fails, a.max_step_fails))
        if not air.alive:
            bits.append("SOCKET CLOSED")
        return "  ".join(bits)

    try:
        # The whole routine runs on the Air and can take minutes. The bounds
        # above stop it hanging forever; this reports where it is while it
        # runs, once a second, so a slow solve is visible as a slow solve
        # rather than as silence.
        with log.slow("auto-goto", quiet_for=1.0, detail=watching):
            while time.time() < end:
                for e in air.drain_events():
                    ev = e.get("Event", "?")
                    if ev in ("Version", "Station", "PiStatus"):
                        continue
                    seen[0] += 1
                    last[0], last[1] = time.time(), ev
                    log.info("%s %s", ev, {k: v for k, v in e.items()
                                           if k not in ("Event", "Timestamp")})
                    print(time.strftime("%H:%M:%S"), ev, {k: v for k, v in e.items()
                          if k not in ("Event", "Timestamp")}, flush=True)

                    # A solve that never returns is the usual way this hangs: the
                    # Air keeps the routine "working" forever and nothing here is
                    # terminal. Bound each solve individually.
                    if ev == "PlateSolve":
                        st = e.get("state")
                        if st == "start":
                            solve_started = time.time()
                            log.info("plate solve started (cap %.0fs)", a.solve_timeout)
                        elif st in ("complete", "fail"):
                            log.info("plate solve %s after %.1fs", st,
                                     time.time() - solve_started if solve_started else 0.0)
                            solve_started = None

                    # The real failure signal is AutoGotoStep, NOT AutoGoto: the
                    # step reports {'state': 'fail', 'code': 252} while the parent
                    # AutoGoto stays 'working' indefinitely.
                    if ev == "AutoGotoStep" and e.get("state") == "fail":
                        step_fails += 1
                        log.warn("auto-goto step failed: code %s (%d/%d)",
                                 e.get("code"), step_fails, a.max_step_fails)
                        print(f"-> auto-goto step failed "
                              f"(code {e.get('code')}, {step_fails}/{a.max_step_fails})",
                              flush=True)
                        if step_fails >= a.max_step_fails:
                            log.error("giving up after %d step failure(s) in %.0fs",
                                      step_fails, time.time() - t0)
                            print("-> giving up on auto-goto", flush=True)
                            air.call("stop_auto_goto", [], timeout=10)
                            return 3

                    if ev in ("AutoGoto", "GotoComplete") and e.get("state") in ("complete", "fail"):
                        log.info("auto-goto %s after %.0fs (%d event(s))",
                                 e.get("state"), time.time() - t0, seen[0])
                        print(f"\n-> auto-goto {e.get('state')}", flush=True)
                        return 0

                if (a.solve_timeout and solve_started
                        and time.time() - solve_started > a.solve_timeout):
                    log.error("plate solve exceeded %.0fs — aborting", a.solve_timeout)
                    print(f"\n-> plate solve exceeded {a.solve_timeout:.0f}s — aborting",
                          file=sys.stderr, flush=True)
                    air.call("stop_auto_goto", [], timeout=10)
                    return 4
                time.sleep(0.3)
    except KeyboardInterrupt:
        log.warn("stopped watching — the routine keeps running on the Air")
    finally:
        air.close()
    log.error("hit --seconds (%ds) with no terminal event after %d event(s)",
              a.seconds, seen[0])
    print("(stopped watching — hit --seconds without a terminal event)")
    return 5


if __name__ == "__main__":
    sys.exit(main())
