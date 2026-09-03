#!/usr/bin/env python3
"""Bring the Air back after it reboots mid-session, in the order that matters.

An Air reboot drops the camera and detaches the mount, but leaves the AM5N
powered and holding its own frame -- so this deliberately does NOT re-home.
Homing would throw away a pointing frame that is usually still good, and cost a
sync budget we may need. Check the mount's clock: year 2000 means it really did
power-cycle and a full restore_mount.py is needed instead.
"""
import datetime, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from air_rpc import Air
from airlog import get_logger

log = get_logger("recover")
HOST = os.environ.get("ASIAIR_HOST", "192.168.1.35")
KEY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "embedded_key.pem")
LAT, LON = 55.689444, 12.555278


def main():
    print("1. Air clock (falls back to 2019-02-14 on boot)")
    a = Air(HOST, 4700, key=KEY, timeout=20)
    n = datetime.datetime.now(datetime.UTC)
    r = a.call("pi_set_time", [{"year": n.year, "mon": n.month, "day": n.day,
                                "hour": n.hour, "min": n.minute, "sec": n.second,
                                "time_zone": "UTC"}], timeout=12)
    print(f"   pi_set_time -> code {r.get('code')}")

    print("2. attaching the mount")
    m = Air(HOST, 4400, timeout=15)
    m.call("set_connected", [{"mount": True, "async": True}], timeout=15)
    t0 = time.time()
    info = None
    while time.time() - t0 < 45:
        try:
            resp = m.call("scope_get_info", [], timeout=10)
            if "result" in resp:
                info = resp["result"]; break
        except Exception:
            pass
        time.sleep(2)
    if not info:
        print("   MOUNT DID NOT ATTACH — check USB / power"); return 1
    print(f"   attached in {time.time()-t0:.1f}s")

    utc = info.get("utc_time", "")
    if utc.startswith("2000"):
        print(f"   !! mount clock is {utc} — it POWER-CYCLED.")
        print("      Its frame is gone. Run restore_mount.py (homes the mount).")
        return 2
    print(f"   mount clock {utc} — kept power, frame intact")

    print("3. site location")
    loc = m.call("scope_get_location", [], timeout=10).get("result")
    if not loc or abs(loc[0] - LAT) > 0.01:
        m.call("scope_set_location", [LAT, LON], timeout=10)
        print(f"   restored -> {LAT}, {LON}")
    else:
        print(f"   intact {loc}")

    print("4. tracking")
    if not info.get("is_enable_track"):
        m.call("scope_set_track_state", [True], timeout=12)
        print("   was OFF — re-enabled")
    else:
        print("   already on")

    print("5. main camera (a BARE open_camera grabs the GUIDE sensor)")
    # After a reboot the Air can hold a half-dead handle that makes open_camera
    # return 0 while the state stays "close". Close it first, always.
    try:
        a.call("close_camera", [], timeout=15)
        time.sleep(2.0)
    except Exception:
        pass
    a.call("open_camera", ["ZWO ASI585MC Air"], timeout=40)
    t0 = time.time()
    while time.time() - t0 < 30:
        st = a.call("get_camera_state", [], timeout=10).get("result", {})
        if st.get("state") != "close":
            print(f"   {st.get('name')} -> {st.get('state')} in {time.time()-t0:.1f}s"); break
        time.sleep(1.5)
    else:
        print("   camera did not open"); return 1

    hz = m.call("scope_get_horiz_coord", [], timeout=10)["result"]
    print(f"\n   ready — pointing alt {hz[0]:.2f} az {hz[1]:.2f}, "
          f"{info.get('input_voltage',0)/1000:.2f} V")
    print("   NOTE: register is unverified until the next successful solve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
