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

from air_rpc import Air
from mount import Mount

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
    ap.add_argument("--force", action="store_true", help="skip the horizon check")
    a = ap.parse_args()

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
    if alt < a.min_alt and not a.force:
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
    air.call("open_camera", [main["name"]], timeout=25)
    print(f"main camera: {main['name']}")

    params = [a.ra, a.dec, a.angle]   # matches startAutoGoto(ra, dec, angle)
    print("start_auto_goto ->", air.call("start_auto_goto", params, timeout=15).get("result"))

    print(f"\nwatching events up to {a.seconds}s (Ctrl-C to stop watching; the "
          f"routine keeps running on the Air):")
    end = time.time() + a.seconds
    try:
        while time.time() < end:
            for e in air.drain_events():
                ev = e.get("Event", "?")
                if ev in ("Version", "Station", "PiStatus"):
                    continue
                print(time.strftime("%H:%M:%S"), ev, {k: v for k, v in e.items()
                      if k not in ("Event", "Timestamp")})
                if ev in ("AutoGoto", "GotoComplete") and e.get("state") in ("complete", "fail"):
                    print(f"\n-> auto-goto {e.get('state')}")
                    return 0
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        air.close()
    print("(stopped watching)")


if __name__ == "__main__":
    sys.exit(main())
