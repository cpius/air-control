#!/usr/bin/env python3
"""Walk the focuser monotonically in one direction across the counter's
limits, re-labelling with `reset_step` whenever the counter would run out,
and measure the blur scale (focus_blurscale) at every step. Prints positions on
an absolute scale (offset tracked through every re-label) so the result is one
continuous curve.

    python3 focus/focus_descend.py --direction down --step 20000 --stop-after-rise 2 --limit -60000 --offset 160000

`--offset` is what to add to the counter to get the absolute (old) scale at
start. Ends by moving to the best position seen.
"""
import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from session import Session
from focuscompare import move_to
from focus_blurscale import blur_scale


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--key", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "embedded_key.pem"))
    ap.add_argument("--direction", choices=["down", "up"], default="down")
    ap.add_argument("--step", type=int, default=20000)
    ap.add_argument("--limit", type=int, required=True, help="absolute position to stop at")
    ap.add_argument("--offset", type=int, required=True, help="absolute = counter + offset, at start")
    ap.add_argument("--stop-after-rise", type=int, default=2)
    ap.add_argument("--exp", type=float, default=0.5)
    ap.add_argument("--gain", type=int, default=0)
    ap.add_argument("--frames", type=int, default=2)
    ap.add_argument("--maxc", type=int, default=100000, help="counter ceiling")
    a = ap.parse_args()
    sgn = -1 if a.direction == "down" else 1
    s = Session(a.host, a.key, with_mount=False)
    try:
        s.c("open_focuser", [0])
        counter = int(s.c("get_focuser_position"))
        offset = a.offset
        print("start counter %d = absolute %d" % (counter, counter + offset), flush=True)
        s.page("focus", a.exp, a.gain, binning=2)
        time.sleep(a.exp + 1.5)
        hist = []
        best = None
        rises = 0
        while True:
            target = counter + sgn * a.step
            if target < 0 or target > a.maxc:
                # re-label: put the counter in the middle so we can keep going
                newc = a.maxc // 2
                r = s.c("set_focuser_value", ["reset_step", newc])
                time.sleep(1)
                got = int(s.c("get_focuser_position"))
                if got != newc:
                    print("re-label failed (%s, counter %d) -- stopping" % (r, got)); break
                offset += counter - newc
                counter = newc
                target = counter + sgn * a.step
                print("re-labelled: counter %d, absolute = counter + %d" % (counter, offset), flush=True)
            move_to(s, target, timeout=120)
            counter = int(s.c("get_focuser_position"))
            absolute = counter + offset
            s.fresh(1)
            vals = []
            for _ in range(a.frames):
                v, w, h = s.fresh(1)
                raw = np.frombuffer(v, dtype=np.uint8)
                img = raw.reshape(h, w) if raw.size == w * h else raw.view(np.uint16).reshape(h, w)
                r, snr = blur_scale(img)
                if r is not None:
                    vals.append((r, float(np.median(img))))
            if not vals:
                print("  abs %7d: no measurement" % absolute, flush=True)
            else:
                r = float(np.median([x[0] for x in vals])); med = vals[0][1]
                hist.append((absolute, r, counter, offset))
                print("  abs %7d (counter %6d): blur %5.1f superpx   median %6.0f" % (absolute, counter, r, med), flush=True)
                if best is None or r < best[1]:
                    best = (absolute, r, counter, offset); rises = 0
                elif r > best[1]:
                    rises += 1
                if rises >= a.stop_after_rise and best is not None:
                    print("blur rising for %d steps past the minimum at abs %d" % (rises, best[0]), flush=True); break
            if (sgn < 0 and absolute <= a.limit) or (sgn > 0 and absolute >= a.limit):
                print("reached limit abs %d" % absolute, flush=True); break
        if best is not None:
            want = best[0] - offset
            if 0 <= want <= a.maxc:
                move_to(s, want, timeout=200)
            print("best absolute %d (blur %.1f); counter now %s, absolute = counter + %d" % (best[0], best[1], s.c("get_focuser_position"), offset), flush=True)
        s.c("stop_exposure")
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
