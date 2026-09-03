#!/usr/bin/env python3
"""Live HFD readout on the FOCUS page -- ~0.4s per reading instead of ~4s.

The focus page free-runs and the newest frame is always on 4800, so there is no
per-frame exposure round trip and no event to wait for (see guidefocus_fast.py).
That makes it about ten times more responsive under a hand on the dial.

Its absolute numbers are NOT comparable with the preview-page ones: it serves a
1472x830 field that plate-solved 42.6' away from the main camera's, so it is a
different set of stars. Measured back-to-back on 2026-08-27 the same defocus read
18.5 px here against 28.5 px on preview, a factor of 0.65 -- so the preview-page
best of 12.49 px corresponds to roughly 8.1 px on this page. Trend is what
matters; confirm the final number on preview.
"""
import os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from guidefocus_fast import FocusPage
import guidefocus as gf

EXP  = float(os.environ.get("GF_EXP", 2.0))
GAIN = int(os.environ.get("GF_GAIN", 300))
SCALE = 0.653
TARGET = 8.1          # focus-page equivalent of the 12.49 px preview best

def main():
    fp = FocusPage(binning=1)
    fp.set_exposure(EXP, GAIN)
    time.sleep(EXP + 1.5)
    print("focus page, %.1fs gain %d — LOWER IS BETTER, target ~%.1f px" % (EXP, GAIN, TARGET), flush=True)
    prev = None
    while True:
        try:
            a, w, h = fp.frame(fresh=True, timeout=10)
            src, bg, sig = gf.sources(a, w, h, min_sep=80, top=20)
            good = [s for s in src if not s[3] and s[2] > bg + 1200]
            hf = []
            for x, y, val, _ in good[:8]:
                m = gf.hfd(a.ravel().tolist(), w, h, x, y, ap=40)
                if m and 1.0 < m["hfd"] < 70:
                    hf.append(m["hfd"])
            if len(hf) < 2:
                print("  (only %d star(s) measurable)" % len(hf), flush=True)
                time.sleep(0.3); continue
            med = float(np.median(hf))
            arrow = "" if prev is None else (
                "  BETTER" if med < prev - 0.3 else ("  worse" if med > prev + 0.3 else "  ="))
            bar = "#" * max(1, min(50, int(med)))
            print("HFD %6.2f px  %6.2f arcsec  n=%2d %-8s %s" %
                  (med, med * SCALE, len(hf), arrow, bar), flush=True)
            prev = med
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("  frame error: %s" % str(e)[:70], flush=True); time.sleep(1.0)
    fp.close()

if __name__ == "__main__":
    main()
