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
    python3 mount.py goto <ra_h> <dec_d>  # slew, waits for it to land
                                          # (no horizon check — caller decides)
    python3 mount.py sync <ra_h> <dec_d>  # sync pointing model
    python3 mount.py park                  # send home, wait for it to land
    python3 mount.py abort                 # stop a slew in progress
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from air_rpc import Air
from airlog import add_log_args, configure_logging, get_logger

log = get_logger("mount")

PORT = 4400  # mount + guide channel, unauthenticated


class Mount:
    def __init__(self, host, port=PORT, timeout=10):
        self.air = Air(host, port, timeout=timeout)  # no key: 4400 has no auth

    def _r(self, method, params=None):
        reply = self.air.call(method, params or [], timeout=15)
        if isinstance(reply, dict) and reply.get("error"):
            log.error("%s -> %s (code %s)", method, reply["error"], reply.get("code"))
            raise RuntimeError(f"{method}: {reply['error']} (code {reply.get('code')})")
        return reply.get("result") if isinstance(reply, dict) else reply

    # --- attach / detach ---
    def connected(self):
        """True if the Air currently has the mount attached."""
        r = self.air.call("get_connected", [], timeout=10)
        res = r.get("result") if isinstance(r, dict) else None
        return isinstance(res, dict) and "mount" in res

    def connect(self, restore=None):
        """Attach the mount.

        Same call as the guide camera, different field — there is no
        `set_mount` / `open_mount` (both are method-not-found). Nothing else
        attaches it: `select_mount_list_index` picks the *driver* and refuses
        with "cannot select if equipment are connected" while one is attached.

        An Air restart drops the mount, so this is what you need after one.

        CAUTION: attaching WIPES the site location to [0, 0] and nothing warns
        you — every alt/az, horizon check and polar-align result is then quietly
        wrong (the tell: at Dec 90 the reported Alt is 0, not your latitude).
        Pass `restore=(lat, lon)` to put it straight back.
        """
        log.info("attaching the mount (set_connected mount=True)")
        r = self._r("set_connected", [{"mount": True}])
        t0 = time.time()
        ok = [False]
        # The attach settles asynchronously; up to 15 s of nothing is normal,
        # and a mount that is not cabled looks exactly the same until it times
        # out — so report the wait every second.
        with log.slow("waiting for the mount to attach", quiet_for=1.0,
                      detail=lambda: "attached" if ok[0] else "not attached yet"):
            while time.time() - t0 < 15:
                if self.connected():
                    ok[0] = True
                    break
                time.sleep(0.5)
        if ok[0]:
            log.info("mount attached after %.1fs", time.time() - t0)
        else:
            log.warn("mount still not attached after %.1fs", time.time() - t0)
        if restore:
            log.info("restoring the site to lat=%s lon=%s (attaching zeroes it)",
                     restore[0], restore[1])
            self._hold_location(restore)
        return r

    def _hold_location(self, site, hold_s=6.0, deadline_s=30.0):
        """Write the site and keep writing until it STAYS written.

        The attach settles asynchronously and zeroes the location seconds after
        set_connected has already returned — well after a naive write-and-verify
        has confirmed success. So require the value to survive `hold_s` of
        re-checking before believing it.
        """
        end = time.time() + deadline_s
        good_since = None
        writes = [0]
        state = ["starting"]
        with log.slow("holding the site at %.4f, %.4f" % (site[0], site[1]),
                      quiet_for=1.0,
                      detail=lambda: "%s, %d rewrite(s)" % (state[0], writes[0])):
            while time.time() < end:
                time.sleep(1.5)
                try:
                    ok, lat, lon = self.check_location()
                except RuntimeError:
                    state[0] = "mount not answering yet"
                    good_since = None          # mount not ready yet
                    continue
                if ok and abs(lat - site[0]) < 1e-3 and abs(lon - site[1]) < 1e-3:
                    good_since = good_since or time.time()
                    state[0] = "held for %.1fs of %.1fs" % (
                        time.time() - good_since, hold_s)
                    if time.time() - good_since >= hold_s:
                        log.info("site held at %.4f, %.4f for %.0fs — it stuck",
                                 lat, lon, hold_s)
                        return True
                else:
                    good_since = None
                    state[0] = "read back [%s, %s] — rewriting" % (lat, lon)
                    log.warn("site read back as [%s, %s], not [%s, %s] — "
                             "writing it again", lat, lon, site[0], site[1])
                    writes[0] += 1
                    try:
                        self.set_location(*site)
                    except RuntimeError as e:
                        log.warn("scope_set_location failed: %s", e)
        log.error("site would not stay set after %.0fs and %d rewrite(s) — every "
                  "alt/az from here is suspect", deadline_s, writes[0])
        return False

    def disconnect(self):
        return self._r("set_connected", [{"mount": False}])

    def reconnect(self):
        """Detach and re-attach, preserving the site location across the cycle."""
        saved = None
        try:
            ok, lat, lon = self.check_location()
            if ok:
                saved = (lat, lon)
        except Exception:
            pass
        log.info("reconnecting the mount (site to preserve: %s)", saved)
        self.disconnect()
        with log.slow("waiting 2s after detach", quiet_for=1.0):
            time.sleep(2)
        return self.connect(restore=saved)

    def ensure_connected(self, verbose=True, restore=None):
        """Attach the mount if it is not already. Returns True if it had to."""
        if self.connected():
            log.debug("mount is already attached")
            return False
        log.info("mount not attached — connecting")
        if verbose:
            print("mount not attached — connecting ...")
        self.connect(restore=restore)
        if not self.connected():
            raise RuntimeError("could not attach the mount — is it powered and "
                               "cabled to the Air? (mount_scan_port lists ports)")
        return True

    # --- site location ---
    def location(self):
        """[lat, lon] in degrees."""
        return self._r("scope_get_location")

    def set_location(self, lat, lon):
        return self._r("scope_set_location", [float(lat), float(lon)])

    def check_location(self):
        """An Air restart, or attaching the mount, can silently zero the site.
        Sanity test: at Dec 90 the altitude must equal the latitude.
        Returns (ok, lat, lon)."""
        lat, lon = self.location()
        return (abs(lat) > 0.01 or abs(lon) > 0.01), lat, lon

    # --- reads (safe) ---
    def info(self):        return self._r("get_connected_mount_info")
    def mount_list(self):  return self._r("get_mount_list")
    def index(self):       return self._r("get_mount_index")
    def cap(self):         return self._r("scope_get_cap")
    def state(self):       return self._r("scope_get_info")
    def ra_dec(self):      return self._r("scope_get_ra_dec")       # [ra_h, dec_d, sidereal]
    def horiz(self):       return self._r("scope_get_horiz_coord")  # [alt, az]
    def tracking(self):    return self._r("scope_get_track_state")

    # --- waiting on motion ---
    def _wait_event(self, name, timeout=120.0, poll=0.2, on_progress=None,
                    keepalive=5.0):
        """Block until Event `name` reports state="complete". Returns the event.

        The Air streams progress on the same socket as the RPC replies:
        repeated {"Event": "ScopeHome", "state": "working", "lapse_ms": N,
        "RA": .., "Dec": ..} and finally the same with state="complete"
        (packet capture 2026-08-15; ScopeGoto has an identical shape).

        Do NOT wait on `is_home_succeed` from scope_get_info instead — it stays
        False even after a confirmed "complete", so polling it just burns the
        whole timeout. Position is no good either: park is a re-home routine,
        not a monotonic approach (Dec ran 87 -> 88.3 -> 86.8 -> 89.2 -> 90), and
        the target position is reached fractionally *before* "complete".

        The keepalive is not optional padding: 4400 hangs up a connection that
        has sent nothing for ~15s (measured: dead at 15.1s). A slew longer than
        that outlives its own socket, so a purely passive listener goes deaf
        mid-move and then waits out the full timeout on a dead connection. Any
        request resets the idle timer. This is why a short slew appears to work
        and a long one does not — testing on a 5s park will NOT catch it.

        Raises TimeoutError if the completion never arrives, ConnectionError if
        the socket dies in a way the keepalive cannot fix.
        """
        end = time.time() + timeout
        next_ka = time.time() + keepalive
        last = None
        t0 = time.time()
        seen = [0]
        state = ["no progress event yet"]

        def where():
            ev = last or {}
            bits = [state[0]]
            if "RA" in ev or "Dec" in ev:
                bits.append("RA=%.4f Dec=%.4f" % (ev.get("RA", float("nan")),
                                                  ev.get("Dec", float("nan"))))
            bits.append("%d event(s)" % seen[0])
            return "  ".join(bits)

        # A slew is tens of seconds and the mount is moving the whole time.
        # Reporting where it is once a second is the difference between "still
        # slewing" and "it stopped and nobody noticed".
        log.info("waiting for %s (timeout %.0fs, keepalive every %.0fs)",
                 name, timeout, keepalive)
        with log.slow("%s" % name, quiet_for=1.0, detail=where):
            while time.time() < end:
                for ev in self.air.drain_events():
                    if ev.get("Event") != name:
                        continue
                    last = ev
                    seen[0] += 1
                    state[0] = ev.get("state", "?")
                    log.debug("%s %s at %.1fs: %s", name, ev.get("state"),
                              ev.get("lapse_ms", 0) / 1000.0,
                              json.dumps({k: v for k, v in ev.items()
                                          if k not in ("Event", "Timestamp")},
                                         ensure_ascii=False)[:160])
                    if on_progress:
                        on_progress(ev)
                    if ev.get("state") == "complete":
                        log.info("%s complete after %.1fs (%d progress event(s))",
                                 name, time.time() - t0, seen[0])
                        return ev
                if not self.air.alive:
                    log.error("4400 closed mid-%s after %.1fs — the mount is "
                              "probably still moving", name, time.time() - t0)
                    raise ConnectionError(
                        f"the Air closed the 4400 connection while waiting for {name}"
                        " — the mount is probably still moving; re-check with 'coord'")
                if time.time() >= next_ka:
                    # 4400 hangs up an idle socket at ~15s; any request resets it.
                    log.debug("keepalive on 4400 (%.0fs into the wait)",
                              time.time() - t0)
                    self.air.call("scope_get_ra_dec", [], timeout=10)
                    next_ka = time.time() + keepalive
                time.sleep(poll)
        log.error("%s never reported complete within %.0fs (%d event(s) seen)",
                  name, timeout, seen[0])
        raise TimeoutError(
            f"{name} did not report state=complete within {timeout:.0f}s"
            + (f"; last progress: {json.dumps(last, ensure_ascii=False)}" if last
               else f" (no {name} events seen at all)"))

    # --- control (moves hardware) ---
    def set_tracking(self, on):     return self._r("scope_set_track_state", [bool(on)])
    def goto(self, ra_h, dec_d, wait=True, timeout=120.0, on_progress=None):
        """Slew to RA (hours) / Dec (degrees). No horizon check — caller decides.

        Waits for the ScopeGoto completion by default, for the same reason park
        does: the RPC returns 0 on *acceptance*. Falls back to a position check
        if the event never arrives, mirroring park.

        Don't be tempted to substitute "have the coordinates stopped moving?"
        for the event — Dec reaches the target several seconds before RA does
        (measured: Dec settled at 12.4s, RA still tracking in at 14.4s), so a
        naive settle-check returns mid-slew and the next command then fails
        with `operation is in progress` (code 268).
        """
        log.info("goto RA=%.4fh Dec=%.4f (wait=%s, timeout=%.0fs)",
                 ra_h, dec_d, wait, timeout)
        try:
            st = self.state()
            log.info("  from RA=%.4f Dec=%.4f Alt=%.2f Az=%.2f",
                     st["RA"], st["Dec"], st["Alt"], st["Az"])
        except Exception as e:
            log.debug("could not read the start position: %s", e)
        self.air.drain_events()      # stale ScopeGoto would complete instantly
        r = self._r("scope_goto", [float(ra_h), float(dec_d)])
        log.debug("scope_goto accepted -> %s", r)
        if not wait:
            log.info("not waiting — the slew continues on the mount")
            return r
        try:
            self._wait_event("ScopeGoto", timeout=timeout, on_progress=on_progress)
        except TimeoutError as e:
            st = self.state()
            if (abs(st["Dec"] - float(dec_d)) < 0.05
                    and abs(st["RA"] - float(ra_h)) < 0.02
                    and st.get("move_status") in (None, "none")):
                log.warn("no ScopeGoto completion, but the mount is on target "
                         "and idle — treating it as arrived")
                print(f"WARNING: {e}\n  ... but the mount is at RA "
                      f"{st['RA']:.4f} Dec {st['Dec']:.4f} and idle, so "
                      f"treating it as arrived.", file=sys.stderr)
                return r
            raise
        return r

    def sync(self, ra_h, dec_d):    return self._r("scope_sync", [float(ra_h), float(dec_d)])
    def move(self, direction):      return self._r("scope_move", [str(direction)])
    def park(self, wait=True, timeout=120.0, on_progress=None):
        """Send the mount to its HOME / zero position (Dec 90).

        On the AM5N park == home: this is exactly what the app's "Go Home"
        button sends (verified by packet capture 2026-08-10 --
        {"id":N,"method":"scope_park"} with no params). The Air then emits
        ScopeHome progress events until state="complete". Use this to rebuild
        the pointing reference after the mount has been physically moved.

        With wait=True this returns once the Air reports the home move complete,
        not merely once the command was accepted. Budget real time for it: a
        1.5 s hop from an already-homed position is not representative, and a
        60 deg return took 21 s on an AM5N. Falls back to a position check if
        the completion event never shows up, so a firmware that stops emitting
        ScopeHome degrades to the old behaviour rather than raising on a mount
        that did in fact get home.
        """
        log.info("park / Go Home (wait=%s, timeout=%.0fs)", wait, timeout)
        try:
            st = self.state()
            log.info("  from Dec=%.4f Alt=%.2f", st["Dec"], st["Alt"])
        except Exception as e:
            log.debug("could not read the start position: %s", e)
        self.air.drain_events()      # stale ScopeHome would complete instantly
        r = self._r("scope_park")
        log.debug("scope_park accepted -> %s", r)
        if not wait:
            log.info("not waiting — the home move continues on the mount")
            return r
        try:
            self._wait_event("ScopeHome", timeout=timeout, on_progress=on_progress)
        except TimeoutError as e:
            st = self.state()
            if abs(st["Dec"] - 90.0) < 0.05 and st.get("move_status") in (None, "none"):
                log.warn("no ScopeHome completion, but the mount is at Dec 90 "
                         "and idle — treating it as homed")
                print(f"WARNING: {e}\n  ... but the mount is at Dec "
                      f"{st['Dec']:.4f} and idle, so treating it as homed.",
                      file=sys.stderr)
                return r
            raise
        return r
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
    sub.add_parser("list"); sub.add_parser("abort")
    pk = sub.add_parser("park", help="send the mount home and wait for it to land")
    pk.add_argument("--no-wait", action="store_true",
                    help="return as soon as the Air accepts the command")
    pk.add_argument("--timeout", type=float, default=120.0)
    cn = sub.add_parser("connect")
    cn.add_argument("--lat", type=float, help="restore the site after connecting")
    cn.add_argument("--lon", type=float)
    sub.add_parser("disconnect"); sub.add_parser("reconnect")
    loc = sub.add_parser("location")
    loc.add_argument("lat", type=float, nargs="?"); loc.add_argument("lon", type=float, nargs="?")
    t = sub.add_parser("track"); t.add_argument("state", choices=["on", "off"])
    g = sub.add_parser("goto", help="slew to RA/Dec and wait for it to land")
    g.add_argument("ra", type=float); g.add_argument("dec", type=float)
    g.add_argument("--no-wait", action="store_true",
                   help="return as soon as the Air accepts the command")
    g.add_argument("--timeout", type=float, default=120.0)
    y = sub.add_parser("sync"); y.add_argument("ra", type=float); y.add_argument("dec", type=float)
    m = sub.add_parser("move"); m.add_argument("direction")
    add_log_args(ap)
    a = ap.parse_args()
    configure_logging(a)
    log.info("mount %s -> %s:%d", a.cmd, a.host, PORT)

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
            t0 = time.time()
            show = lambda ev: print(f"  [{ev.get('lapse_ms', 0)/1000:5.1f}s] "
                                    f"{ev.get('state'):<8} RA={ev.get('RA'):.4f} "
                                    f"Dec={ev.get('Dec'):.4f}", flush=True)
            print(f"goto RA={a.ra}h Dec={a.dec}° ->",
                  mnt.goto(a.ra, a.dec, wait=not a.no_wait, timeout=a.timeout,
                           on_progress=show))
            if not a.no_wait:
                st = mnt.state()
                print(f"arrived in {time.time() - t0:.1f}s: "
                      f"RA={st['RA']:.4f} Dec={st['Dec']:.4f} "
                      f"Alt={st['Alt']:.2f} Az={st['Az']:.2f}")
        elif a.cmd == "sync":
            print(f"sync RA={a.ra}h Dec={a.dec}° ->", mnt.sync(a.ra, a.dec))
        elif a.cmd == "move":
            print("move ->", mnt.move(a.direction))
        elif a.cmd == "park":
            t0 = time.time()
            show = lambda ev: print(f"  [{ev.get('lapse_ms', 0)/1000:5.1f}s] "
                                    f"{ev.get('state'):<8} Dec={ev.get('Dec'):.4f}",
                                    flush=True)
            print("park ->", mnt.park(wait=not a.no_wait, timeout=a.timeout,
                                      on_progress=show))
            if not a.no_wait:
                st = mnt.state()
                print(f"home reached in {time.time() - t0:.1f}s: "
                      f"Dec={st['Dec']:.4f} Alt={st['Alt']:.2f} "
                      f"tracking={mnt.tracking()}")
        elif a.cmd == "connect":
            restore = (a.lat, a.lon) if a.lat is not None and a.lon is not None else None
            was = mnt.ensure_connected(restore=restore)
            print("connected" if was else "already connected", "->",
                  json.dumps(mnt.info(), ensure_ascii=False))
            ok, lat, lon = mnt.check_location()
            print(f"site: lat={lat} lon={lon}" + ("" if ok else
                  "   <-- WARNING: site is zeroed; set it with 'location <lat> <lon>'"))
        elif a.cmd == "disconnect":
            print("disconnect ->", mnt.disconnect())
        elif a.cmd == "reconnect":
            print("reconnect ->", mnt.reconnect())
            ok, lat, lon = mnt.check_location()
            print(f"site preserved: lat={lat} lon={lon} ok={ok}")
        elif a.cmd == "location":
            if a.lat is None:
                ok, lat, lon = mnt.check_location()
                print(f"lat={lat} lon={lon}" + ("" if ok else "   <-- zeroed"))
            else:
                print("set_location ->", mnt.set_location(a.lat, a.lon))
        elif a.cmd == "abort":
            print("abort ->", mnt.abort())
    finally:
        mnt.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        log.error("%s", e)
        print("ERROR:", e, file=sys.stderr); sys.exit(1)
