#!/usr/bin/env python3
"""A stand-in ASIAIR, so the dashboard can be built and tested with the rig packed away.

Speaks enough of the 4700/4400 protocol to exercise every path the recorder
taps: replies with results, replies with errors, timeouts that never answer, and
the unprompted event stream that carries all real progress. It scripts a plausible
night -- connect, slew, solve, focus sweep, guide, expose -- so panes can be
laid out against data of the right shape and rate rather than against invented
JSON.

It is a test double, NOT a simulator: the numbers are plausible, not physical,
and nothing here should ever be used to check an astronomy result. Its only job
is to make the plumbing observable while the sky is unavailable.

    python3 dashboard/fake_air.py --port 4700 &
    python3 lib/air_rpc.py --host 127.0.0.1 --port 4700 call get_device_state
"""

import argparse
import json
import random
import socket
import threading
import time

# Deterministic: the same night every run, so a dashboard change is the only
# thing that can move a pixel between two screenshots.
RNG = random.Random(20260821)

RESULTS = {
    "test_connection": "server connected",
    "pi_is_verified": True,
    "get_device_state": {
        "camera": {"name": "ASI585MC", "connected": True, "temperature": -9.8,
                   "cooler_on": True, "cool_power": 63},
        "mount": {"name": "AM5N", "connected": True, "tracking": True,
                  "ra": 20.5906, "dec": 60.2414, "side": "west"},
        "focuser": {"name": "EAF", "connected": True, "position": 11077,
                    "temperature": 14.2},
        "guider": {"name": "ASI220MM", "connected": True, "state": "guiding"},
    },
    "get_power_supply": {"volt": 12957, "amp": 1510, "watt": 19.6},
    "scope_get_equ_coord": {"ra": 20.5906, "dec": 60.2414},
    "get_focuser_position": 11077,
    "start_solve": 0,
    "get_last_solve_result": {"state": "complete", "ra": 20.5912, "dec": 60.2402,
                              "rotation": 178.4, "image_id": 4471, "star_number": 214},
}

# Methods that answer with an error, and the ones that never answer at all --
# both are states the dashboard has to render, and both are common in practice.
ERRORS = {"set_plan": (103, "method not found"),
          "start_auto_goto": (300, "internal error")}
BLACKHOLE = {"stuck_method"}


def script(t):
    """The event timeline, as (delay_seconds, event_dict)."""
    ev = []

    def at(dt, name, **kw):
        ev.append((dt, dict(Event=name, **kw)))

    at(0.3, "PiStatus", is_undervolt=False, temp=48.2, is_over_current=False)
    at(0.6, "AutoGoto", state="start", ra=20.5906, dec=60.2414)
    for i, s in enumerate(("slewing", "slewing", "settling")):
        at(0.9 + i * 0.4, "AutoGotoStep", state=s, count=i + 1)
    at(2.2, "AutoGoto", state="complete", ra=20.5912, dec=60.2402)
    at(2.5, "PlateSolve", state="start")
    at(3.4, "PlateSolve", state="complete", ra=20.5912, dec=60.2402,
       star_number=214, duration=0.9, image_id=4471)
    # A focus sweep: HFD falls to a minimum and rises again, as a V-curve must.
    for i in range(9):
        pos = 10877 + i * 50
        hfd = round(abs(pos - 11077) / 105.0 + 3.7 + RNG.uniform(-0.06, 0.06), 2)
        at(3.8 + i * 0.25, "AutoFocus", state="step", position=pos, hfd=hfd, count=i + 1)
    at(6.2, "AutoFocus", state="complete", position=11077, hfd=3.71)
    at(6.5, "Exposure", state="start", exp_us=120000000)
    at(6.9, "GuideStar", x=612.4, y=388.1, hfd=2.9)
    for i in range(14):
        at(7.0 + i * 0.2, "GuideStep",
           RADistanceRaw=round(RNG.gauss(0, 0.42), 3),
           DECDistanceRaw=round(RNG.gauss(0, 0.38), 3),
           RADuration=RNG.randint(0, 180), DECDuration=RNG.randint(0, 140),
           SNR=round(RNG.uniform(18, 34), 1), Frame=i + 1)
    at(9.9, "Exposure", state="complete", exp_us=120000000, image_id=4472)
    at(10.2, "SettleDone", Status=0, TotalFrames=8, Time=6.1)
    at(10.6, "PiStatus", is_undervolt=True, temp=51.9, is_over_current=False)
    at(11.0, "TotallyNewEventName", note="proves unknown events still surface")
    return ev


def serve(conn, addr, port, loop):
    log = lambda *a: print("  fake_air[%s]" % port, *a)
    log("client", addr)
    t0 = time.time()
    pending = list(script(t0))
    buf = b""
    conn.settimeout(0.05)
    while True:
        # Events first: they are unprompted, which is the whole character of
        # this channel and the thing a request/response mock gets wrong.
        now = time.time() - t0
        while pending and pending[0][0] <= now:
            _, e = pending.pop(0)
            e["Timestamp"] = "%.3f" % now
            try:
                conn.sendall((json.dumps(e) + "\r\n").encode())
            except OSError:
                return
        if not pending and not loop:
            pass
        try:
            b = conn.recv(65536)
            if not b:
                log("client gone"); return
            buf += b
        except socket.timeout:
            continue
        except OSError:
            return
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                continue
            m, rid = req.get("method"), req.get("id")
            if m in BLACKHOLE:
                log("swallowing", m); continue
            time.sleep(RNG.uniform(0.02, 0.12))       # a Pi is never instant
            if m in ERRORS:
                code, msg = ERRORS[m]
                rep = {"id": rid, "jsonrpc": "2.0", "error": msg, "code": code}
            else:
                rep = {"id": rid, "jsonrpc": "2.0",
                       "result": RESULTS.get(m, 0)}
            rep["Timestamp"] = "%.3f" % (time.time() - t0)
            try:
                conn.sendall((json.dumps(rep) + "\r\n").encode())
            except OSError:
                return


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--port", type=int, default=4700)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--loop", action="store_true", help="restart the timeline per client")
    a = p.parse_args()
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((a.host, a.port))
    s.listen(5)
    print("fake ASIAIR on %s:%d" % (a.host, a.port))
    while True:
        c, addr = s.accept()
        threading.Thread(target=serve, args=(c, addr, a.port, a.loop),
                         daemon=True).start()


if __name__ == "__main__":
    main()
