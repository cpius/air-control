#!/usr/bin/env python3
"""Wait for a mount power-cycle, then re-attach and fix its clock.

Polls scope_get_info every --every seconds with a heartbeat. A power-cycle shows as the
mount dropping off the Air (code 314/315) and, once re-attached, a clock reading year 2000.
When that happens: set_connected(mount), restore the site, scope_set_time from this Mac,
verify sidereal_time against a computed LST, then exit 0 so the caller can home and goto.

    ASIAIR_HOST=192.168.1.35 python3 -u rig/mountwatch.py --lat 55.6896 --lon 12.5551 --max-wait 900
"""
import argparse, datetime as dt, json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from air_rpc import Air

ap = argparse.ArgumentParser()
ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST", "192.168.1.35"))
ap.add_argument("--lat", type=float, default=55.6896); ap.add_argument("--lon", type=float, default=12.5551)
ap.add_argument("--every", type=float, default=4.0); ap.add_argument("--max-wait", type=float, default=900)
a = ap.parse_args()

def now(): return time.strftime("%H:%M:%S")
def lst_hours(utc, lon_deg):
    d = utc.timestamp() / 86400.0 + 2440587.5 - 2451545.0
    return ((18.697374558 + 24.06570982441908 * d) % 24.0 + lon_deg / 15.0) % 24.0
def res(r): return r.get("result", r.get("error", r)) if isinstance(r, dict) else r

mt = None
def call(m, p=None, t=12):
    global mt
    if mt is None:
        mt = Air(a.host, 4400)
    try:
        return res(mt.call(m, p or [], timeout=t))
    except Exception as e:
        try: mt.close()
        except Exception: pass
        mt = None
        return f"EXC {e.__class__.__name__}"

t0 = time.time(); gone_seen = False; n = 0
while time.time() - t0 < a.max_wait:
    n += 1
    info = call("scope_get_info")
    if isinstance(info, dict):
        utc = info.get("utc_time", "")
        if gone_seen or utc.startswith("2000-"):
            print(f"{now()} mount back, clock reads {utc} -> POWER-CYCLE CONFIRMED" if utc.startswith("2000-") else f"{now()} mount back (was gone), clock {utc}", flush=True)
            break
        print(f"{now()} +{time.time()-t0:4.0f}s mount attached, clock {utc}, RA {info.get('RA'):.3f} Dec {info.get('Dec'):.3f} — waiting for the power-cycle", flush=True)
    else:
        code = info.get("code") if isinstance(info, dict) else None
        print(f"{now()} +{time.time()-t0:4.0f}s mount not attached: {json.dumps(info)[:120]} -> re-attaching", flush=True)
        gone_seen = True
        r = call("set_connected", [{"mount": True}], t=20)
        print(f"{now()}   set_connected -> {json.dumps(r)[:120]}", flush=True)
    time.sleep(a.every)
else:
    print(f"{now()} gave up after {a.max_wait:.0f}s", flush=True); sys.exit(2)

print(f"{now()} restoring site {a.lat},{a.lon}: {call('scope_set_location', [a.lat, a.lon])}", flush=True)
u = dt.datetime.now(dt.timezone.utc)
print(f"{now()} scope_set_time -> {call('scope_set_time', [f'{u.year}-{u.month}-{u.day}T{u.hour}:{u.minute}:{u.second}', '0'])}", flush=True)
time.sleep(1.0)
info = call("scope_get_info")
if isinstance(info, dict):
    lst = lst_hours(dt.datetime.now(dt.timezone.utc), a.lon); d = ((info["sidereal_time"] - lst + 12) % 24) - 12
    print(f"{now()} clock {info.get('utc_time')}  sidereal {info['sidereal_time']:.4f} vs computed {lst:.4f} ({d*60:+.1f} min)  site {info.get('Lat'):.4f},{info.get('Lon'):.4f}  track {info.get('is_enable_track')}", flush=True)
print(f"{now()} READY — now: mount.py park ; mount.py track on ; mount.py goto", flush=True)
