#!/usr/bin/env python3
"""Measure the Air's AVI recorder throughput: frame period vs ROI size, exposure,
and whether a client pulling preview frames over 4800 slows it.

Uses exactly satvideo.py's recording sequence (rtmp page, stop_exposure,
set_subframe, exposure/gain, start_exposure(light), start/stop_record_avi) but
never touches the mount. Each case records --seconds of whatever the camera
sees; the resulting AVIs (in Video/ on the Air's share) carry the measured fps
in their header, and the sidecar .txt the exposure.

    python3 -u calibrate/fpstest.py --seconds 12 --gain 350
"""
import argparse, os, sys, time, threading
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from daypipes import host, Pipes, log

ap = argparse.ArgumentParser()
ap.add_argument("--seconds", type=float, default=12.0)
ap.add_argument("--gain", type=int, default=350)
ap.add_argument("--cases", default="320x240@5,640x480@5,800x800@5,1000x1000@5,1920x1080@5,1000x1000@20,1000x1000@46,1000x1000@46+grab",
                help="comma list of WxH@exp_ms, optional +grab to pull 4800 frames during the recording")
a = ap.parse_args()
CHIP = (3840, 2160)

p = Pipes()
try:
    log("open_camera -> %s ; state %s" % (p.c("open_camera", ["ZWO ASI585MC Air"]), p.c("get_camera_state")))
    p.setup("rtmp", 0.005, a.gain, 1)
    for case in a.cases.split(","):
        grab = case.endswith("+grab"); case = case.replace("+grab", "")
        size, exp = case.split("@"); w, h = (int(v) for v in size.split("x")); exp_ms = float(exp)
        p.c("stop_exposure"); time.sleep(0.8)
        want = {"x": (CHIP[0] - w) // 2, "y": (CHIP[1] - h) // 2, "width": w, "height": h}
        r = p.c("set_subframe", [want]); sf = p.c("get_subframe")
        if not (isinstance(sf, dict) and sf.get("width") == w and sf.get("height") == h):
            r = p.c("set_subframe", want); sf = p.c("get_subframe")
        p.c("set_control_value", ["Exposure", int(exp_ms * 1000)]); p.c("set_control_value", ["Gain", a.gain]); p.exp = exp_ms / 1000.0
        log("case %s: subframe %s exposure %.1f ms gain %d" % (case + ("+grab" if grab else ""), sf, exp_ms, a.gain))
        p.s.air.drain_events(); p.last_sig = None
        log("  start_exposure -> %s" % p.c("start_exposure", ["light"]))
        time.sleep(4.0)                                   # let the stream settle
        stop = threading.Event(); grabs = [0]
        def puller():
            while not stop.is_set():
                try:
                    p.grab(timeout=10); grabs[0] += 1
                except Exception as e:
                    log("  grab error %s" % e); time.sleep(1)
        th = None
        if grab:
            th = threading.Thread(target=puller, daemon=True); th.start()
        t0 = time.time(); log("  start_record_avi -> %s" % p.c("start_record_avi"))
        while time.time() - t0 < a.seconds:
            time.sleep(1.0)
        log("  stop_record_avi -> %s after %.1f s%s" % (p.c("stop_record_avi"), time.time() - t0, (" ; %d frames pulled over 4800" % grabs[0]) if grab else ""))
        stop.set()
        if th: th.join(timeout=12)
        time.sleep(2.0)
    p.c("stop_exposure")
    log("done; camera left idle on the rtmp page")
finally:
    try: p.close()
    except Exception: pass
