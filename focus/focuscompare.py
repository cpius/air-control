#!/usr/bin/env python3
"""Verify a focus position in about two minutes, instead of sweeping blind.

Most of the design here is about not being fooled:

  * **PREVIEW page at bin 2** — the whole field at 1:1 in CFA superpixels. The
    focus page's centre crop can be empty on a sparse field while the full
    field has hundreds of stars, and bin 2 is a quarter of the download. On a
    Bayer sensor a 2x2 sum IS the superpixel, so the mosaic never enters the
    profile; measuring raw preview pixels instead leaves a hard checker that
    swamps every width.
  * **Positions visited ROUND-ROBIN.** Seeing moves by ~1" over a few minutes,
    which is larger than the difference being measured, so a sequential sweep
    scores the weather. Interleave and compare per round.
  * **Two metrics that must agree** — HFD (lower better) and peak (higher
    better). Flux is conserved as focus changes, so a tighter star is simply
    taller. If they disagree the run is noise; take it again.
  * **A side-by-side 1:1 crop** of the same star at each position, because the
    picture settles an argument a table does not.

IT PUTS THE FOCUSER BACK ON THE WINNER before exiting. Leaving it parked
wherever the sweep happened to end is its own bug: well off focus, the plate
solver stops solving entirely, which looks nothing like a focus problem.

    python3 focus/focuscompare.py --pos 10800,11050,11300
"""
import argparse
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from session import Session, write_png

HALF = 28          # crop half-size, in binned pixels
ZOOM = 4


def move_to(s, pos, timeout=30.0):
    s.c("move_focuser", [int(pos)])
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.5)
        st = s.c("get_focuser_state")
        if isinstance(st, dict) and st.get("state") == "idle":
            return True
    return False


def detect(v, w, h, nmax=10, edge=24, sat=60000):
    """Brightest unsaturated stars. Threshold from a MAD sigma, not a
    percentile — a star-rich field pushes any high percentile up and quietly
    raises the bar."""
    s = sorted(v[::533])
    bg = s[len(s) // 2]
    mad = sorted(abs(x - bg) for x in s)
    sig = max(1.0, 1.4826 * mad[len(mad) // 2])
    thr = bg + 8 * sig
    cand = [(v[y * w + x], x, y) for y in range(edge, h - edge)
            for x in range(edge, w - edge) if v[y * w + x] > thr]
    cand.sort(reverse=True)
    out, used = [], []
    for pk, x, y in cand:
        if pk >= sat:                      # a clipped core makes width a lie
            continue
        if any((x - a) ** 2 + (y - b) ** 2 < 400 for a, b in used):
            continue
        used.append((x, y))
        out.append((pk, x, y))
        if len(out) >= nmax:
            break
    return bg, out


def hfd(v, w, h, cx, cy, r=10, rin=12, rout=16):
    """Half-flux diameter against a LOCAL annulus background.

    A global background is not good enough: the aperture multiplies any offset
    by its area, so a few ADU of sky gradient decides the answer on its own.
    """
    ann = []
    for y in range(max(0, cy - rout), min(h, cy + rout + 1)):
        for x in range(max(0, cx - rout), min(w, cx + rout + 1)):
            d2 = (x - cx) ** 2 + (y - cy) ** 2
            if rin * rin <= d2 <= rout * rout:
                ann.append(v[y * w + x])
    if not ann:
        return float("nan")
    ann.sort()
    lb = ann[len(ann) // 2]
    pix, tot = [], 0.0
    for y in range(max(0, cy - r), min(h, cy + r + 1)):
        for x in range(max(0, cx - r), min(w, cx + r + 1)):
            d = v[y * w + x] - lb
            if d > 0:
                rr = math.hypot(x - cx, y - cy)
                if rr <= r:
                    pix.append((rr, d))
                    tot += d
    if tot <= 0:
        return float("nan")
    pix.sort()
    acc = 0.0
    for rr, d in pix:
        acc += d
        if acc >= tot / 2:
            return 2 * rr
    return float("nan")


def _tile(v, w, h, cx, cy, lo, hi):
    rows = []
    for yy in range(cy - HALF, cy + HALF):
        row = []
        for xx in range(cx - HALF, cx + HALF):
            q = v[yy * w + xx] if 0 <= yy < h and 0 <= xx < w else lo
            row.append(min(255, int(255 * max(0.0, (q - lo) / float(max(hi - lo, 1))) ** 0.45)))
        rows.append(row)
    return rows


def render(tiles, path):
    """One strip, left to right, nearest-neighbour zoomed."""
    if not tiles:
        return None
    n = 2 * HALF * ZOOM
    W, H = n * len(tiles), n
    g = bytearray(W * H * 3)
    for i, rows in enumerate(tiles):
        for y in range(H):
            for x in range(n):
                t = rows[y // ZOOM][x // ZOOM]
                j = 3 * (y * W + i * n + x)
                g[j] = g[j + 1] = g[j + 2] = t
        for y in range(H):                          # separator
            j = 3 * (y * W + i * n)
            g[j] = g[j + 1] = g[j + 2] = 90
    return write_png(path, W, H, bytes(g))


def compare(s, positions, exp=2.0, gain=250, rounds=2, png_path=None,
            arcsec_per_px=None, say=print):
    """Round-robin the positions. Returns {pos: {hfd, peak}}."""
    s.c("open_focuser", [0])
    s.page("preview", exp, gain, binning=2)
    acc = {p: {"hfd": [], "peak": []} for p in positions}
    tiles = {}
    for rnd in range(rounds):
        for p in positions:
            move_to(s, p)
            v, w, h = s.fresh(2)          # the frame on the Air predates the move
            bg, st = detect(v, w, h)
            best, ds = None, []
            for pk, x, y in st:
                d = hfd(v, w, h, x, y)
                if d != d or d <= 0:
                    continue
                ds.append(d)
                if best is None or pk > best[0]:
                    best = (pk, x, y)
            if best is None:
                say("  round %d  %6d   no stars" % (rnd + 1, p))
                continue
            ds.sort()
            med = ds[len(ds) // 2]
            acc[p]["hfd"].append(med)
            acc[p]["peak"].append(best[0])
            extra = ("" if arcsec_per_px is None
                     else " = %5.2f\"" % (med * arcsec_per_px))
            say("  round %d  %6d   HFD %5.2f px%s   peak %6d   (%d stars)"
                % (rnd + 1, p, med, extra, best[0], len(ds)))
            tiles[p] = _tile(v, w, h, best[1], best[2], bg, best[0])
    out = {}
    for p in positions:
        hs, ps = sorted(acc[p]["hfd"]), sorted(acc[p]["peak"])
        if hs:
            out[p] = {"hfd": hs[len(hs) // 2], "peak": ps[len(ps) // 2]}
    if png_path:
        render([tiles[p] for p in positions if p in tiles], png_path)
    return out


def verdict(res):
    """Winner, and whether the two metrics actually agree on it."""
    if not res:
        return None, "no measurements"
    by_hfd = min(res, key=lambda p: res[p]["hfd"])
    by_peak = max(res, key=lambda p: res[p]["peak"])
    if by_hfd == by_peak:
        return by_hfd, "HFD and peak agree"
    return by_hfd, ("HFD says %d but peak says %d — the metrics disagree, so this "
                    "is noise; repeat before trusting it" % (by_hfd, by_peak))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--key", default="embedded_key.pem")
    ap.add_argument("--pos", required=True, help="comma-separated focuser positions")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--exp", type=float, default=2.0)
    ap.add_argument("--gain", type=int, default=250)
    ap.add_argument("--arcsec-per-px", type=float, default=None,
                    help="binned pixel scale, to print HFD in arcsec as well "
                         "(a plate solve reports the FOV this comes from)")
    ap.add_argument("--png", default="focus-compare.png")
    ap.add_argument("--no-restore", action="store_true",
                    help="leave the focuser where the sweep ended (do not)")
    a = ap.parse_args()
    if not a.host:
        sys.exit("need --host or ASIAIR_HOST (the Air's IP moves — run discover.py)")
    positions = [int(p) for p in a.pos.split(",")]

    s = Session(a.host, a.key, with_mount=False)
    try:
        res = compare(s, positions, exp=a.exp, gain=a.gain, rounds=a.rounds,
                      png_path=a.png, arcsec_per_px=a.arcsec_per_px)
        win, note = verdict(res)
        print("\n=== %s ===" % note)
        for p in positions:
            if p in res:
                print("  %6d   HFD %5.2f px   peak %6d%s"
                      % (p, res[p]["hfd"], res[p]["peak"],
                         "   <-- best" if p == win else ""))
        if a.png and os.path.exists(a.png):
            print("\nstrip: %s" % a.png)
        if win is not None and not a.no_restore:
            move_to(s, win)
            print("focuser left at %d" % win)
    finally:
        s.close()


if __name__ == "__main__":
    main()
