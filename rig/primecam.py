#!/usr/bin/env python3
"""After an Air boot the camera holds no image and get_current_img says 'there is no image now' forever.
Reopen the 585 (enumerate first, same connection), then one preview start_exposure(["light"]) to `complete`.

    ASIAIR_HOST=192.168.1.39 python3 -u rig/primecam.py --exp 1
"""
import argparse, json, os, sys, time
AC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # the repo root
sys.path.insert(0, os.path.join(AC, "lib"))
from air_rpc import Air
ap = argparse.ArgumentParser(); ap.add_argument("--exp", type=float, default=1.0); ap.add_argument("--reopen", action="store_true")
a = ap.parse_args()
T0 = time.time()
air = Air(os.environ["ASIAIR_HOST"], 4700, key=os.path.join(AC, "embedded_key.pem"), timeout=15)
def c(n, p=None):
    r = air.call(n, p if p is not None else [], timeout=20)
    print("%s %+5.1fs %s %s -> %s" % (time.strftime("%H:%M:%S"), time.time() - T0, n, json.dumps(p) if p else "", json.dumps(r.get("result", r.get("error", r)))[:200]), flush=True); return r
c("stop_exposure")
if a.reopen:
    c("close_camera"); time.sleep(2); c("get_connected_cameras")
    for k in range(5):
        if not c("open_camera", ["ZWO ASI585MC Air"]).get("error"): break
        time.sleep(2); c("get_connected_cameras")
    time.sleep(3)
c("get_camera_state"); c("set_page", ["preview"])
c("set_control_value", ["Exposure", int(a.exp * 1e6)])
air.drain_events()
c("start_exposure", ["light"])
t = time.time(); seen = []
while time.time() - t < a.exp + 60:
    for e in air.drain_events():
        if e.get("Event") in ("Exposure", "Solve", "PlateSolve"):
            seen.append((e.get("Event"), e.get("state"))); print("   event %s %s" % (e.get("Event"), e.get("state")), flush=True)
    if ("Exposure", "complete") in seen: break
    time.sleep(0.5)
    if int(time.time() - t) % 5 == 0: print("   ... %.0fs" % (time.time() - t), flush=True); time.sleep(0.5)
c("stop_solve"); c("get_camera_state")
air.close()
print("primed" if ("Exposure", "complete") in seen else "NOT primed", flush=True)
