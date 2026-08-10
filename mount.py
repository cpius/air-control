#!/usr/bin/env python3
"""Control the AM5N mount through the ASIAIR — on TCP 4400, not 4700.

The big surprise from reverse-engineering the app: mount (and guiding) commands
live on a SEPARATE service on port 4400, which needs **no authentication** (the
RSA handshake is a 4700 thing). That's why every mount method returned
`103 method-not-found` on 4700 — wrong port, not an auth/session gate.

Method names + param shapes lifted from the ASIAIR app's MountGateway
(see RPC_METHODS.md). Coordinates: RA in hours, Dec/Alt/Az in degrees.

    python3 mount.py info                 # model / firmware / selected driver
    python3 mount.py coord                # live RA/Dec/Alt/Az + tracking
    python3 mount.py track on|off         # sidereal tracking
    python3 mount.py goto <ra_h> <dec_d>  # slew (asks nothing — caller decides)
    python3 mount.py sync <ra_h> <dec_d>  # sync pointing model
    python3 mount.py park                  # park; 'abort' stops a slew
"""

import argparse
import json
import os
import sys

from air_rpc import Air

PORT = 4400  # mount + guide channel, unauthenticated


class Mount:
    def __init__(self, host, port=PORT, timeout=10):
        self.air = Air(host, port, timeout=timeout)  # no key: 4400 has no auth

    def _r(self, method, params=None):
        reply = self.air.call(method, params or [], timeout=15)
        if isinstance(reply, dict) and reply.get("error"):
            raise RuntimeError(f"{method}: {reply['error']} (code {reply.get('code')})")
        return reply.get("result") if isinstance(reply, dict) else reply

    # --- reads (safe) ---
    def info(self):        return self._r("get_connected_mount_info")
    def mount_list(self):  return self._r("get_mount_list")
    def index(self):       return self._r("get_mount_index")
    def cap(self):         return self._r("scope_get_cap")
    def state(self):       return self._r("scope_get_info")
    def ra_dec(self):      return self._r("scope_get_ra_dec")       # [ra_h, dec_d, sidereal]
    def horiz(self):       return self._r("scope_get_horiz_coord")  # [alt, az]
    def tracking(self):    return self._r("scope_get_track_state")

    # --- control (moves hardware) ---
    def set_tracking(self, on):     return self._r("scope_set_track_state", [bool(on)])
    def goto(self, ra_h, dec_d):    return self._r("scope_goto", [float(ra_h), float(dec_d)])
    def sync(self, ra_h, dec_d):    return self._r("scope_sync", [float(ra_h), float(dec_d)])
    def move(self, direction):      return self._r("scope_move", [str(direction)])
    def park(self):
        """Send the mount to its HOME / zero position (Dec 90).

        On the AM5N park == home: this is exactly what the app's "Go Home"
        button sends (verified by packet capture 2026-08-10 --
        {"id":N,"method":"scope_park"} with no params). The Air then emits
        ScopeHome progress events until state="complete". Use this to rebuild
        the pointing reference after the mount has been physically moved.
        """
        return self._r("scope_park")
    def abort(self):                return self._r("scope_abort_slew")

    def close(self):
        self.air.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info"); sub.add_parser("coord"); sub.add_parser("caps")
    sub.add_parser("list"); sub.add_parser("park"); sub.add_parser("abort")
    t = sub.add_parser("track"); t.add_argument("state", choices=["on", "off"])
    g = sub.add_parser("goto"); g.add_argument("ra", type=float); g.add_argument("dec", type=float)
    y = sub.add_parser("sync"); y.add_argument("ra", type=float); y.add_argument("dec", type=float)
    m = sub.add_parser("move"); m.add_argument("direction")
    a = ap.parse_args()

    mnt = Mount(a.host)
    try:
        if a.cmd == "info":
            print(json.dumps(mnt.info(), indent=2, ensure_ascii=False))
        elif a.cmd == "coord":
            st = mnt.state()
            print(json.dumps(st, indent=2, ensure_ascii=False))
            print("tracking:", mnt.tracking())
        elif a.cmd == "caps":
            print(json.dumps(mnt.cap(), ensure_ascii=False))
        elif a.cmd == "list":
            print(json.dumps(mnt.mount_list(), ensure_ascii=False))
            print("selected index:", mnt.index())
        elif a.cmd == "track":
            print("set_tracking ->", mnt.set_tracking(a.state == "on"))
        elif a.cmd == "goto":
            print(f"goto RA={a.ra}h Dec={a.dec}° ->", mnt.goto(a.ra, a.dec))
        elif a.cmd == "sync":
            print(f"sync RA={a.ra}h Dec={a.dec}° ->", mnt.sync(a.ra, a.dec))
        elif a.cmd == "move":
            print("move ->", mnt.move(a.direction))
        elif a.cmd == "park":
            print("park ->", mnt.park())
        elif a.cmd == "abort":
            print("abort ->", mnt.abort())
    finally:
        mnt.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print("ERROR:", e, file=sys.stderr); sys.exit(1)
