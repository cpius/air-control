#!/usr/bin/env python3
"""Manual focus check on ONE bright star, on the FOCUS page (1:1 centre crop,
no auto-annotate, ~0.35 s a frame). No autofocus. Positions are visited
round-robin so seeing changes do not masquerade as a focus curve; HFD (lower)
and peak (higher) must agree. The exposure is auto-shortened until the star is
unsaturated. Ends by putting the focuser on the winner.

    python3 focus/focusstar.py --goto 20.70569 45.37671 --pos 10791,10891,10991,11091,11191
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from session import Session
from focuscompare import move_to, detect, hfd


def brightest(v, w, h, unsat=False, avoid=None):
    """Brightest star. With unsat=True: the brightest star whose 5x5 core is
    unsaturated, at least `avoid` px away from the saturated one -- for when
    the target star is far too bright to measure."""
    if not unsat:
        bg, st = detect(v, w, h, nmax=3, edge=12, sat=10 ** 9)
        return bg, (max(st) if st else None)
    bg, st = detect(v, w, h, nmax=40, edge=24, sat=10 ** 9)
    sat = [(x, y) for pk, x, y in st if pk >= 60000]
    good = []
    for pk, x, y in st:
        if pk >= 60000:
            continue
        core = max(v[(y + dy) * w + (x + dx)] for dy in (-2, -1, 0, 1, 2) for dx in (-2, -1, 0, 1, 2))
        if core >= 60000:
            continue
        if avoid and any((x - sx) ** 2 + (y - sy) ** 2 < avoid ** 2 for sx, sy in sat):
            continue
        good.append((pk, x, y))
    return bg, (max(good) if good else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--key", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "embedded_key.pem"))
    ap.add_argument("--goto", nargs=2, type=float, metavar=("RA_H", "DEC_D"))
    ap.add_argument("--pos", required=True)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--frames", type=int, default=3, help="frames averaged per visit")
    ap.add_argument("--exp", type=float, default=0.2)
    ap.add_argument("--gain", type=int, default=100)
    ap.add_argument("--arcsec-per-px", type=float, default=0.473)
    ap.add_argument("--unsat", action="store_true", help="measure the brightest UNSATURATED star, fixed exposure (no auto-exposure)")
    ap.add_argument("--avoid", type=int, default=80, help="px radius to keep clear of a saturated star")
    a = ap.parse_args()
    positions = [int(p) for p in a.pos.split(",")]

    s = Session(a.host, a.key, with_mount=True)
    try:
        start = s.c("get_focuser_position")
        print("focuser at %s" % start, flush=True)
        if a.goto:
            print("goto RA %.4fh Dec %+.3f" % tuple(a.goto), flush=True)
            s.mount().goto(a.goto[0], a.goto[1])
            time.sleep(3)
        s.c("open_focuser", [0])
        exp = a.exp
        s.page("focus", exp, a.gain, binning=1)
        # auto-exposure on the brightest star
        for _ in range(0 if a.unsat else 8):
            v, w, h = s.fresh(2)
            bg, st = brightest(v, w, h)
            if st is None:
                print("no star in the focus crop (frame %dx%d, bg %d)" % (w, h, bg), flush=True)
                return 1
            pk = st[0]
            print("  exp %.3fs: frame %dx%d bg %d  star peak %d at (%d,%d)" % (exp, w, h, bg, pk, st[1], st[2]), flush=True)
            if pk > 50000:
                exp = max(0.001, exp / 3)
            elif pk < 12000:
                exp = min(5.0, exp * 2)
            else:
                break
            s.set_exp(exp, a.gain)
            time.sleep(exp + 0.5)
        print("using exposure %.3fs gain %d" % (exp, a.gain), flush=True)

        acc = {p: {"hfd": [], "peak": []} for p in positions}
        print("%-6s %-5s %8s %8s   %s" % ("round", "pos", "HFD\"", "peak", "per-frame HFD px"), flush=True)
        for rnd in range(a.rounds):
            order = positions if rnd % 2 == 0 else positions[::-1]
            for p in order:
                move_to(s, p)
                s.fresh(1)                       # the frame on the Air predates the move
                ds, pks = [], []
                for _ in range(a.frames):
                    v, w, h = s.fresh(1)
                    bg, st = brightest(v, w, h, unsat=a.unsat, avoid=a.avoid)
                    if st is None:
                        continue
                    d = hfd(v, w, h, st[1], st[2])
                    if d == d and d > 0:
                        ds.append(d); pks.append(st[0])
                if not ds:
                    print("%-6d %-5d   no star" % (rnd, p), flush=True); continue
                ds.sort(); pks.sort()
                md = ds[len(ds) // 2]; mp = pks[len(pks) // 2]
                acc[p]["hfd"].append(md); acc[p]["peak"].append(mp)
                print("%-6d %-5d %8.2f %8d   %s   star@(%d,%d)" % (rnd, p, md * a.arcsec_per_px, mp,
                                                    " ".join("%.2f" % x for x in ds), st[1], st[2]), flush=True)
        print()
        print("%-5s %8s %8s  n" % ("pos", "HFD\"", "peak"))
        summary = []
        for p in positions:
            if acc[p]["hfd"]:
                hs = sorted(acc[p]["hfd"]); ps = sorted(acc[p]["peak"])
                mh, mp = hs[len(hs) // 2], ps[len(ps) // 2]
                summary.append((mh, -mp, p))
                print("%-5d %8.2f %8d  %d" % (p, mh * a.arcsec_per_px, mp, len(hs)))
        if not summary:
            print("nothing measured; restoring %s" % start); move_to(s, start); return 1
        by_hfd = min(summary)[2]
        by_peak = min(summary, key=lambda t: t[1])[2]
        win = by_hfd
        print("best by HFD %d, by peak %d -> %s" % (by_hfd, by_peak, "AGREE" if by_hfd == by_peak else "DISAGREE (noise?)"))
        move_to(s, win)
        s.c("stop_exposure")
        print("focuser left at %d" % win)
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
