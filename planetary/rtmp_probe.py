#!/usr/bin/env python3
"""rtmp page, attempt sequence: reset the leftover subframe, then bin 1, then a
longer exposure. Stop at the first configuration that streams frames."""
import os, sys, time, hashlib, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from daypipes import Pipes, log
p = Pipes(); c = p.c

def events():
    for e in p.s.air.drain_events():
        if e.get("Event") not in ("Temperature", "Station", "PiStatus"):
            log("    EVENT %s" % json.dumps(e)[:220])

def watch(seconds, stop_after=6):
    t0 = time.time(); last = None; n = 0; nnew = 0; tl = t0; sizes = set()
    while time.time() - t0 < seconds:
        events()
        try:
            hdr, files = p.s.img.get_image("get_current_img", 0)
            raw = next(iter(files.values())); n += 1
            sig = hashlib.md5(raw[::997]).hexdigest()
            if sig != last:
                nnew += 1; last = sig
                depth = "8-bit" if len(raw) == hdr["width"]*hdr["height"] else "16-bit"
                sizes.add((hdr["width"], hdr["height"], depth))
                log("    frame #%d NEW at %.1fs: %dx%d %s" % (nnew, time.time()-t0, hdr["width"], hdr["height"], depth))
                if nnew >= stop_after:
                    break
        except Exception as ex:
            log("    download error: %s" % ex); time.sleep(1)
        if time.time() - tl > 10:
            tl = time.time(); log("    ... %.0fs %d reads %d new" % (time.time()-t0, n, nnew))
        time.sleep(0.2)
    log("  -> %d reads, %d new frames in %.0fs  %s" % (n, nnew, time.time()-t0, sorted(sizes)))
    return nnew

def to_preview():
    c("stop_solve"); c("stop_exposure"); time.sleep(0.8)
    c("set_page", ["preview"]); time.sleep(0.8)

def try_rtmp(label, exp, gain):
    c("stop_exposure"); time.sleep(0.5)
    log("%s: set_page rtmp -> %s" % (label, c("set_page", ["rtmp"]))); time.sleep(1.0)
    c("set_control_value", ["Exposure", int(exp*1e6)]); c("set_control_value", ["Gain", gain])
    log("  bin=%s subframe=%s exp=%s" % (c("get_camera_bin"), c("get_subframe"), c("get_control_value", ["Exposure"])["value"]))
    p.s.air.drain_events()
    log("  start_exposure([light]) -> %s" % c("start_exposure", ["light"]))
    time.sleep(1.5); events()
    n = watch(40)
    c("stop_exposure")
    return n

try:
    to_preview()
    log("A: on preview page: bin=%s subframe=%s" % (c("get_camera_bin"), c("get_subframe")))
    log("A: set_camera_bin 1 -> %s" % c("set_camera_bin", [1]))
    log("A: set_subframe full -> %s" % c("set_subframe", [{"x": 0, "y": 0, "width": 3840, "height": 2160}]))
    log("A: subframe now %s" % c("get_subframe"))
    log("A: set_camera_bin 2 -> %s ; subframe now %s" % (c("set_camera_bin", [2]), c("get_subframe")))
    if try_rtmp("B (bin2, full subframe, 1.5ms)", 0.0015, 0) >= 2:
        sys.exit(0)
    to_preview()
    log("C: set_camera_bin 1 -> %s ; subframe %s" % (c("set_camera_bin", [1]), c("get_subframe")))
    if try_rtmp("C (bin1, full subframe, 1.5ms)", 0.0015, 0) >= 2:
        sys.exit(0)
    to_preview()
    if try_rtmp("D (bin1, 20ms)", 0.020, 0) >= 2:
        sys.exit(0)
    to_preview()
    log("E: set_subframe 1920x1080 centre -> %s ; now %s" % (c("set_subframe", [{"x": 960, "y": 540, "width": 1920, "height": 1080}]), c("get_subframe")))
    try_rtmp("E (bin1, 1920x1080 ROI, 5ms)", 0.005, 0)
finally:
    try:
        c("stop_exposure"); c("set_page", ["preview"])
        c("set_camera_bin", [2])
        log("restored: page preview, bin %s, subframe %s" % (c("get_camera_bin"), c("get_subframe")))
    except Exception as ex:
        log("restore failed: %s" % ex)
    p.close(); log("closed")
