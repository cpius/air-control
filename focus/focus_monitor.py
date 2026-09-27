#!/usr/bin/env python3
"""Live focus score for planetary work — you turn the knob, this calls it.

Passive by design. It never sets exposure, gain, bin or page, and it never
moves the focuser: it only reads `get_focuser_position` on 4700 and pulls
whatever frame the Air already has on 4800. So it can run alongside the
ASIAIR app while you focus by hand, which is the whole point — the app's
zoomed live view is better than any number I can print, but it cannot tell
you that step 45188 was tighter than step 45557.

The score is the flux-weighted RMS radius of the brightest blob, in binned
preview pixels. Lower is better. Two reasons it beats peak brightness, which
is the obvious choice and the wrong one:

  * Peak scales with exposure and gain. Change either mid-sweep -- which you
    will, because a defocused planet needs 50 ms and a focused one needs 5 --
    and the peak column becomes meaningless while still looking authoritative.
    RMS radius barely moves.
  * Peak saturates. Once the core clips, the metric stops improving exactly
    where you most need resolution.

RMS is taken over 2x2-binned pixels so the Bayer mosaic (which the Air's
preview subsamples rather than averages, leaving a hard 2-px checker) does
not inflate the width.

DUPLICATE FRAMES ARE THE TRAP. The Air happily serves the same preview frame
several times in a row, and a stale frame after a focuser move reads as "that
move changed nothing". Every row is checksummed and repeats are marked `dup`
and excluded from the running best, so a stalled stream looks like a stalled
stream instead of a flat V-curve.

    python3 focus_monitor.py --host <air-ip> --key embedded_key.pem
    python3 focus_monitor.py --host <air-ip>          # no key: no position column
"""

import argparse
import hashlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from airlog import add_log_args, configure_logging, get_logger
from main_image import MainImage

log = get_logger("focus")

MAIN_PORT = 4700


def _score_numpy(buf, width, height, thr):
    """Same maths as _score_stdlib, ~200x faster. Optional: numpy may be absent."""
    import numpy as np
    a = np.frombuffer(buf, dtype="<u2").reshape(height, width).astype(np.float32)
    h2 = height // 2 * 2
    b = (a[0:h2:2, 0::2] + a[1:h2:2, 0::2] + a[0:h2:2, 1::2] + a[1:h2:2, 1::2])
    med = float(np.median(b))
    s = b - med
    mx = float(s.max())
    if mx <= 0:
        return None
    m = s > thr * mx
    ys, xs = np.nonzero(m)
    w = s[m]
    if w.sum() <= 0 or len(ys) < 5:
        return None
    cx = float((xs * w).sum() / w.sum())
    cy = float((ys * w).sum() / w.sum())
    r2 = float((((ys - cy) ** 2 + (xs - cx) ** 2) * w).sum() / w.sum())
    return {"rms": r2 ** 0.5, "peak": mx, "npix": int(len(ys)),
            "cx": cx, "cy": cy, "sat": int((b >= 262000).sum())}


def score(buf, width, height, thr=0.25):
    """Flux-weighted RMS radius of the brightest blob, in 2x2-binned pixels."""
    try:
        return _score_numpy(buf, width, height, thr)
    except ImportError:
        return _score_stdlib(buf, width, height, thr)


def _score_stdlib(buf, width, height, thr=0.25):
    """Dependency-free fallback. Correct but slow (~1 s a frame) -- fine for a
    hand-paced focus run, too slow to keep up with a fast preview stream."""
    import array
    a = array.array("H")
    a.frombytes(buf)
    if sys.byteorder == "big":
        a.byteswap()
    w2, h2 = width // 2, height // 2
    # 2x2 bin: sum each Bayer quad into one sample.
    b = [0] * (w2 * h2)
    for y in range(h2):
        r0 = 2 * y * width
        r1 = r0 + width
        o = y * w2
        for x in range(w2):
            i = 2 * x
            b[o + x] = a[r0 + i] + a[r0 + i + 1] + a[r1 + i] + a[r1 + i + 1]
    s = sorted(b)
    med = s[len(s) // 2]
    mx = max(b) - med
    if mx <= 0:
        return None
    cut = med + thr * mx
    sw = sx = sy = 0.0
    n = 0
    for y in range(h2):
        o = y * w2
        for x in range(w2):
            v = b[o + x]
            if v > cut:
                wgt = v - med
                sw += wgt
                sx += x * wgt
                sy += y * wgt
                n += 1
    if sw <= 0 or n < 5:
        return None
    cx, cy = sx / sw, sy / sw
    r2 = 0.0
    for y in range(h2):
        o = y * w2
        for x in range(w2):
            v = b[o + x]
            if v > cut:
                r2 += ((x - cx) ** 2 + (y - cy) ** 2) * (v - med)
    return {"rms": (r2 / sw) ** 0.5, "peak": float(mx), "npix": n,
            "cx": cx, "cy": cy, "sat": sum(1 for v in b if v >= 262000)}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ)
    ap.add_argument("--key", help="RSA key — only needed for the focuser position column")
    ap.add_argument("--seconds", type=float, default=600.0)
    ap.add_argument("--arcsec-per-px", type=float, default=None,
                    help="binned-preview pixel scale, to print the blob in arcsec")
    add_log_args(ap)
    args = ap.parse_args()
    configure_logging(args)

    air = None
    if args.key:
        from air_rpc import Air
        air = Air(args.host, MAIN_PORT, key=args.key)
        if not air.verified:
            log.warn("4700 handshake failed — continuing without the position column")
            air = None

    img = MainImage(args.host)
    print("  pos      rms    peak     npix   sat   note")
    print("  -------  -----  -------  -----  ----  ----")

    best = None
    seen = {}
    t_end = time.time() + args.seconds
    try:
        while time.time() < t_end:
            hdr, files = img.get_image()
            raw = list(files.values())[0]
            h = hashlib.md5(raw).hexdigest()[:8]
            dup = h in seen
            seen[h] = True
            sc = score(raw, hdr["width"], hdr["height"])
            pos = ""
            if air is not None:
                try:
                    r = air.call("get_focuser_position", [], timeout=8)
                    pos = str(r.get("result", ""))
                except Exception:
                    pos = "?"
            if sc is None:
                print(f"  {pos:>7}  {'--':>5}  {'--':>7}  {'--':>5}  {'--':>4}  no blob")
                continue
            note = ""
            if dup:
                note = "dup (stale frame — ignored)"
            else:
                if best is None or sc["rms"] < best[0]:
                    best = (sc["rms"], pos)
                    note = "<<< best so far"
                elif best[0] > 0:
                    note = f"best {best[0]:.1f} @ {best[1]}"
            if sc["sat"]:
                note = f"SATURATED ({sc['sat']} px) — lower gain/exposure  {note}"
            extra = ""
            if args.arcsec_per_px:
                # For a roughly uniform disc, rms radius = R/sqrt(2), so the
                # apparent diameter is 2*sqrt(2)*rms. Printing a Gaussian FWHM
                # here would be the wrong model for a planet and would read
                # ~20% small.
                extra = f'  ~{2 * (2 ** 0.5) * sc["rms"] * args.arcsec_per_px:.0f}" across'
            print(f"  {pos:>7}  {sc['rms']:5.1f}  {sc['peak']:7.0f}  {sc['npix']:5d}  "
                  f"{sc['sat']:4d}  {note}{extra}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        img.close()
        if air is not None:
            air.close()
    if best:
        print(f"\n  best rms {best[0]:.1f} at focuser {best[1]}")


if __name__ == "__main__":
    main()
