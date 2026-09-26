#!/usr/bin/env python3
"""Rig status in one call, and (--set-clocks) set the Air and mount clocks from this Mac's UTC.

Reads: mount register + sidereal time (checked against a locally computed LST), mount clock,
EAF position/temperature, Wi-Fi, camera state/subframe. Every RPC name is checked against
CMD_METHODS.tsv first -- never probe names (a 103 proves nothing).

    ASIAIR_HOST=192.168.1.35 python3 -u rigstatus.py --set-clocks
"""
import argparse, datetime as dt, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from air_rpc import Air

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST", "192.168.1.35"))
ap.add_argument("--key", default=os.path.join(HERE, "embedded_key.pem"))
ap.add_argument("--set-clocks", action="store_true", help="push this Mac's UTC to the Air (pi_set_time) and the mount (scope_set_time)")
ap.add_argument("--lon", type=float, default=12.5556)
a = ap.parse_args()

known = {line.split("\t")[0].strip() for line in open(os.path.join(HERE, "CMD_METHODS.tsv"))}

def res(r):
    return r.get("result", r.get("error", r)) if isinstance(r, dict) else r

def call(cli, name, params=None):
    if name not in known:
        print(f"  {name}: NOT in CMD_METHODS.tsv -- skipped"); return None
    try:
        r = res(cli.call(name, params or []))
    except Exception as e:
        r = f"EXC {e!r}"
    s = r if isinstance(r, str) else json.dumps(r)
    print(f"  {name}{' ' + json.dumps(params) if params else ''} -> {s[:400]}")
    return r

def lst_hours(utc, lon_deg):
    jd = utc.timestamp() / 86400.0 + 2440587.5
    d = jd - 2451545.0
    return ((18.697374558 + 24.06570982441908 * d) % 24.0 + lon_deg / 15.0) % 24.0

mt = Air(a.host, 4400)
cam = Air(a.host, 4700, key=a.key)
now = dt.datetime.now(dt.timezone.utc)
print(f"local {dt.datetime.now():%H:%M:%S}  UTC {now:%Y-%m-%d %H:%M:%S}  LST(computed) {lst_hours(now, a.lon):.4f} h")
if a.set_clocks:
    print("setting clocks from this Mac:")
    now = dt.datetime.now(dt.timezone.utc)
    call(cam, "pi_set_time", [{"year": now.year, "mon": now.month, "day": now.day, "hour": now.hour, "min": now.minute, "sec": now.second, "time_zone": "UTC"}])
    call(mt, "scope_set_time", [f"{now.year}-{now.month}-{now.day}T{now.hour}:{now.minute}:{now.second}", "0"])
    time.sleep(1.0)
print("mount (4400):")
call(mt, "scope_get_time")
info = call(mt, "scope_get_info")
if isinstance(info, dict):
    lst = lst_hours(dt.datetime.now(dt.timezone.utc), a.lon)
    st = info.get("sidereal_time")
    if st is not None:
        d = ((st - lst + 12) % 24) - 12
        print(f"  sidereal_time {st:.4f} vs computed {lst:.4f}: diff {d*60:+.1f} min -> {'OK' if abs(d*60) < 1 else 'CLOCK WRONG'}")
    print(f"  register RA {info.get('RA')} Dec {info.get('Dec')}  Az {info.get('Az')} Alt {info.get('Alt')}  track {info.get('is_enable_track')}  pier {info.get('pier_side')}  utc_time {info.get('utc_time')}  mV {info.get('input_voltage')}")
print("focuser (4700):")
for n in ("get_connected_focuser", "get_focuser_state", "get_focuser_position", "get_focuser_info"):
    call(cam, n)
print("wifi (4700):")
for n in ("get_wifi", "get_ap"):
    call(cam, n)
print("camera (4700):")
call(cam, "get_camera_state"); call(cam, "get_subframe")
call(cam, "get_control_value", ["Gain"]); call(cam, "get_control_value", ["Exposure"])
call(cam, "get_camera_bin"); call(cam, "pi_get_info")
