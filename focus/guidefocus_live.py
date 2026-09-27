#!/usr/bin/env python3
"""Live HFD readout for focusing the guide sensor by hand.

The guide sensor's only focus control is a knob on the camera body, so this is a
human-in-the-loop instrument: grab, measure, print, repeat, while someone turns
the dial and watches the number fall.

Two design points, both learned the hard way on 2026-08-27:

* THE TARGET STAR IS NOT THE METRIC STAR. Vega saturates this sensor even at
  10 ms / gain 0 (mag 0 through a 200 mm aperture), and a flat-topped star has
  no measurable HFD -- its brightest pixel wanders over the plateau and every
  derived number is noise. Focus is measured on fainter field stars instead.

* STARS ARE LOCKED BY POSITION, not re-picked each frame. "The brightest
  unsaturated star" changes identity as focus changes -- a defocusing star's
  peak falls -- and the metric jumps when the set changes. Locking positions
  means the number reflects focus and nothing else.

Reports the MEDIAN HFD over the locked stars. LOWER IS BETTER.
"""
import math, os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from starhunt import Camera
import guidefocus as gf

HOST = os.environ.get("ASIAIR_HOST", "192.168.1.35")
KEY  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "embedded_key.pem")
EXP  = float(os.environ.get("GF_EXP", 1.0))
GAIN = int(os.environ.get("GF_GAIN", 200))
SCALE = 0.653                                    # arcsec/px, guide sensor

def measure_at(a, w, h, locks):
    out, moved = [], []
    for (lx, ly) in locks:
        x0, x1 = max(0, lx - 30), min(w, lx + 30)
        y0, y1 = max(0, ly - 30), min(h, ly + 30)
        sub = a[y0:y1, x0:x1]
        iy, ix = np.unravel_index(int(np.argmax(sub)), sub.shape)
        cx, cy = x0 + int(ix), y0 + int(iy)
        if a[cy, cx] >= 60000:
            moved.append((cx, cy)); continue
        m = gf.hfd(a.ravel().tolist(), w, h, cx, cy, ap=30)
        if m and m["hfd"] < 60:
            out.append(m["hfd"]); moved.append((cx, cy))
        else:
            moved.append((cx, cy))
    return out, moved

def main():
    cam = Camera(HOST, KEY, "ZWO ASI220MM Air")
    print(f"exposure {EXP}s gain {GAIN}   LOWER HFD IS BETTER", flush=True)
    v, w, h, _ = cam.grab(EXP, GAIN)
    a = np.frombuffer(v, dtype=np.uint16).reshape(h, w)
    src, bg, sig = gf.sources(a, w, h, min_sep=60, top=25)
    sat = [s for s in src if s[3]]
    vx, vy = (np.mean([s[0] for s in sat]), np.mean([s[1] for s in sat])) if sat else (1e9, 1e9)
    locks = [(s[0], s[1]) for s in src
             if not s[3] and math.hypot(s[0]-vx, s[1]-vy) > 200 and s[2] > 9000][:4]
    if not locks:
        print("no usable metric stars — try a longer exposure or a fainter target"); return
    print("locked on %d star(s): %s" % (len(locks), locks), flush=True)
    prev = None
    while True:
        try:
            v, w, h, _ = cam.grab(EXP, GAIN)
            a = np.frombuffer(v, dtype=np.uint16).reshape(h, w)
            vals, locks = measure_at(a, w, h, locks)
            if not vals:
                print("  (no measurable star this frame)", flush=True); continue
            med = float(np.median(vals))
            arrow = "" if prev is None else (
                "  BETTER" if med < prev - 0.15 else
                ("  worse" if med > prev + 0.15 else "  ="))
            print("HFD %6.2f px  %6.2f arcsec   n=%d%s" %
                  (med, med * SCALE, len(vals), arrow), flush=True)
            prev = med
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("  frame failed: %s" % e, flush=True); time.sleep(2)
    cam.close()

if __name__ == "__main__":
    main()
