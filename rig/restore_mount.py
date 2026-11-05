#!/usr/bin/env python3
"""Bring the AM5N back after a factory reset, in the order that matters.

A reset wipes settings, not the polar axis -- that is mechanical. What it does
wipe is time, site, guide rate and track mode, and the Air will not have the
mount attached at all until told to.

Order is deliberate: clocks go in BEFORE homing, because the mount builds its
frame from the clock it has, and homing under a wrong clock is what started a
whole evening of bad pointing.

    python3 rig/restore_mount.py            # restore, then test goto
    python3 rig/restore_mount.py --no-test  # restore only
"""
import argparse, datetime, json, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from air_rpc import Air
from airlog import add_log_args, configure_logging, get_logger

log = get_logger("restore")
HOST = os.environ.get("ASIAIR_HOST", "192.168.1.35")
KEY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "embedded_key.pem")
LAT, LON = 55.689444, 12.555278
GUIDE_RATE = 0.9          # 0.25 silently breaks guide calibration


def step(n, msg):
    print(f"\n{n}. {msg}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--no-test", action="store_true")
    add_log_args(ap)
    a = ap.parse_args()
    configure_logging(a)

    air7 = Air(a.host, 4700, timeout=10, key=KEY)
    m = Air(a.host, 4400, timeout=10)

    step(1, "Air (Pi) clock")
    n = datetime.datetime.now(datetime.timezone.utc)
    r = air7.call("pi_set_time", [{"year": n.year, "mon": n.month, "day": n.day,
                                   "hour": n.hour, "min": n.minute, "sec": n.second,
                                   "time_zone": "UTC"}], timeout=10)
    print(f"   pi_set_time -> code {r.get('code')}")

    step(2, "re-attach the mount the way the ASIAIR app does (2026-10-03 capture)")
    # The app's sequence cleared goto 300 after a power-up where set_connected
    # without "async" and a bare Go Home did not: detach, ~9 s, attach with
    # async, then time + site + Go Home. See docs/RPC_METHODS.md.
    if not m.call("get_connected_mount_info", [], timeout=8).get("error"):
        m.call("set_connected", [{"mount": False}], timeout=12)
        for k in range(9):
            time.sleep(1)
            print(f"   detached {k+1}s", flush=True)
    m.call("set_connected", [{"mount": True, "async": True}], timeout=12)
    t0 = time.time()
    while time.time() - t0 < 30:
        rr = m.call("get_connected_mount_info", [], timeout=8)
        if not rr.get("error"):
            print(f"   attached after {time.time()-t0:.0f}s: {json.dumps(rr['result'])[:110]}")
            break
        print(f"   t+{time.time()-t0:.0f}s waiting (code {rr.get('code')})")
        time.sleep(2)
    else:
        sys.exit("   mount would not attach")

    step(3, "mount clock — BEFORE homing")
    # Exactly the app's shape: zero-padded UTC, and the STANDARD-time zone offset
    # as "%.2f" ("1.00" in Copenhagen even in summer). We used to send "0".
    n = datetime.datetime.now(datetime.timezone.utc)
    m.call("scope_set_time", [n.strftime("%Y-%m-%dT%H:%M:%S"), "%.2f" % (-time.timezone / 3600.0)],
           timeout=10)
    i = m.call("scope_get_info", [], timeout=10)["result"]
    print(f"   utc_time = {i['utc_time']}  time_offset {i.get('time_offset')!r}  (sidereal {i['sidereal_time']:.4f} h)")

    step(4, "site (the app always writes it after attaching)")
    m.call("scope_set_location", [LAT, LON], timeout=10)
    i = m.call("scope_get_info", [], timeout=10)["result"]
    print(f"   lat {i['Lat']:.5f}  lon {i['Lon']:.5f}")

    step(5, "guide rate + tracking")
    m.call("scope_set_guide_rate", [GUIDE_RATE], timeout=10)
    m.call("scope_set_track_state", [True], timeout=10)
    i = m.call("scope_get_info", [], timeout=10)["result"]
    print(f"   guide_rate {i['guide_rate']}  tracking {i['is_enable_track']}  "
          f"{i['input_voltage']/1000.0:.2f} V")

    if a.no_test:
        return

    step(6, "Go Home, then: does scope_goto work again?  (the whole point)")
    m.call("scope_park", [], timeout=10)
    t0 = time.time()
    while time.time() - t0 < 120:
        time.sleep(2)
        s = m.call("scope_get_info", [], timeout=8)["result"]
        print(f"   t+{time.time()-t0:4.0f}s homing  Dec={s['Dec']:.3f}  move={s['move_status']}", flush=True)
        if time.time() - t0 > 4 and s["move_status"] == "none" and abs(s["Dec"] - 90) < 0.01:
            break
    i = m.call("scope_get_info", [], timeout=10)["result"]
    tgt_ra, tgt_dec = (i["RA"] + 0.5) % 24, min(i["Dec"] + 8.0, 80.0)
    print(f"   from RA {i['RA']:.4f} Dec {i['Dec']:.3f} -> RA {tgt_ra:.4f} Dec {tgt_dec:.3f}")
    rr = m.call("scope_goto", [tgt_ra, tgt_dec], timeout=12)
    print(f"   scope_goto accepted: code {rr.get('code')}")
    t0 = time.time()
    moved = False
    while time.time() - t0 < 20:
        time.sleep(2)
        s = m.call("scope_get_info", [], timeout=8)["result"]
        if abs(s["Dec"] - i["Dec"]) > 0.5:
            moved = True
        print(f"   t+{time.time()-t0:4.0f}s move={s['move_status']:6s} "
              f"RA={s['RA']:.4f} Dec={s['Dec']:.3f}")
        if moved and s["move_status"] == "none":
            break
    print("\n   >>> GOTO WORKS — the reset fixed it" if moved else
          "\n   >>> still broken (300/501) — the reset did not fix it")
    m.close(); air7.close()


if __name__ == "__main__":
    main()
