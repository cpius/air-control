#!/usr/bin/env python3
"""Live guide-sensor focus readout that works at ANY defocus.

Replaces the HFD readout, which fails badly far from focus in two ways found on
2026-08-27:

  * IT PEGS. HFD is measured inside a 40 px aperture; once the star is bigger
    than that, the flux fills the aperture uniformly and the half-flux radius
    converges on ap/sqrt(2) = 28.3 px, i.e. HFD 56.6, regardless of how bad the
    focus is. Hours of "55.x, worse/=" were a pegged instrument, not data.
  * IT LATCHES ONTO RIMS. A defocused SCT star is a broad blob with structure;
    the peak finder picks maxima on its rim and measures HFD about a rim point,
    so the value jumps with whichever rim pixel happens to win.

This measures blob AREA above the sky instead, and reports the equivalent
diameter. It cannot peg (no aperture), it does not care where the peak is, and
it degenerates smoothly into a normal star diameter as focus comes in.
"""
import os, sys, time, math
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from guidefocus_fast import FocusPage

EXP   = float(os.environ.get("GF_EXP", 2.0))
GAIN  = int(os.environ.get("GF_GAIN", 300))
SCALE = 0.653

def blobs(a, w, h, k=6.0, min_area=12, max_blobs=8):
    """Connected regions above sky. Returns equivalent diameters, px."""
    bg = float(np.median(a))
    sig = float(np.median(np.abs(a - bg))) * 1.4826 or 1.0
    mask = (a > bg + k * sig).astype(np.uint8)
    try:
        import cv2
        n, lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
        out = []
        for i in range(1, n):
            area = int(stats[i, cv2.CC_STAT_AREA])
            ww, hh = int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT])
            if area < min_area:
                continue
            if ww >= w - 2 or hh >= h - 2:            # background gradient, not a star
                continue
            out.append((area, 2.0 * math.sqrt(area / math.pi)))
        out.sort(reverse=True)
        return [d for _, d in out[:max_blobs]], bg, sig
    except ImportError:
        return [], bg, sig

def main():
    fp = FocusPage(binning=1)
    fp.set_exposure(EXP, GAIN); time.sleep(EXP + 1.5)
    print("blob-diameter readout — LOWER IS BETTER, cannot peg", flush=True)
    prev = None
    while True:
        try:
            a, w, h = fp.frame(fresh=True, timeout=10)
            ds, bg, sig = blobs(a, w, h)
            if len(ds) < 2:
                print("  (%d blob(s) — too few to trust)" % len(ds), flush=True)
                time.sleep(0.2); continue
            med = float(np.median(ds))
            arrow = "" if prev is None else (
                "  BETTER" if med < prev - 0.5 else ("  worse" if med > prev + 0.5 else "  ="))
            print("diam %6.2f px  %6.2f arcsec  n=%2d%-8s %s" %
                  (med, med * SCALE, len(ds), arrow, "#" * max(1, min(60, int(med)))), flush=True)
            prev = med
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("  frame error: %s" % str(e)[:70], flush=True); time.sleep(1.0)
    fp.close()

if __name__ == "__main__":
    main()
