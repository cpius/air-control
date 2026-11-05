#!/usr/bin/env python3
"""Interleaved focus check on lunar surface texture (focus page, bin 1).

Rounds alternate direction so a linear drift in seeing/content cancels; each
position scores the median of --frames frames (one discarded after the move).
Metrics (daypipes.metrics): tenengrad and texture (higher = sharper) must agree.
Parks on --park if given, else on the position that wins both metrics.

    ASIAIR_HOST=192.168.1.36 python3 -u focus/texfocus.py --pos 43760,44260,44760,45260,45760 --rounds 3
"""
import argparse, json, os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from daypipes import host, Pipes, log, save_png, metrics, move_to, focuser_pos

ap = argparse.ArgumentParser()
ap.add_argument("--pos", required=True)
ap.add_argument("--rounds", type=int, default=3)
ap.add_argument("--frames", type=int, default=3)
ap.add_argument("--exp-ms", type=float, default=15.0)
ap.add_argument("--gain", type=int, default=220)
ap.add_argument("--target", type=float, default=30000.0, help="p99.5 to aim the one-time auto-exposure at")
ap.add_argument("--park", type=int, default=None)
ap.add_argument("--outdir", default=os.path.expanduser("~/ASICAP/telemetry/%s/texfocus_%s" % (time.strftime("%Y-%m-%d"), time.strftime("%H%M"))))
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)
pos = [int(v) for v in a.pos.split(",")]

p = Pipes()
rows = []
try:
    start = focuser_pos(p); log("EAF start %d" % start)
    exp = a.exp_ms / 1000.0
    p.setup("focus", exp, a.gain, 1)
    p.grab(timeout=40)
    for it in range(4):
        img, w, h, info = p.grab(timeout=40)
        p995 = float(np.percentile(img, 99.5)); log("auto %d: %.1f ms g%d %dx%d -> p99.5 %.0f max %.0f" % (it, exp * 1e3, a.gain, w, h, p995, img.max()))
        if 0.7 * a.target < p995 < 1.3 * a.target: break
        exp = float(np.clip(exp * a.target / max(p995 - 500, 200), 0.001, 0.2))
        p.c("set_control_value", ["Exposure", int(round(exp * 1e6))]); p.exp = exp
        p.s.air.drain_events(); time.sleep(0.3); p.grab(timeout=40)
    t0 = time.time()
    for r in range(a.rounds):
        order = pos if r % 2 == 0 else pos[::-1]
        for q in order:
            move_to(p, q)
            p.grab(timeout=40)                     # discard: may predate the move
            ten, tex = [], []
            for k in range(a.frames):
                img, w, h, info = p.grab(timeout=40)
                m = metrics(img); ten.append(m["tenengrad"]); tex.append(m["texture"])
                if k == 0:
                    np.save(os.path.join(a.outdir, "r%d_p%d.npy" % (r, q)), img.astype(np.uint16))
                    save_png(img, os.path.join(a.outdir, "r%d_p%d.png" % (r, q)), shrink=2)
            row = dict(round=r, pos=q, t=time.time() - t0, ten=float(np.median(ten)), tex=float(np.median(tex)), p99=float(np.percentile(img, 99)))
            rows.append(row)
            log("  round %d  EAF %6d  ten %7.3f  tex %6.2f  (frames ten %s)  p99 %.0f  t=%.0fs"
                % (r, q, row["ten"], row["tex"], ",".join("%.2f" % v for v in ten), row["p99"], row["t"]))
    # per-round normalisation: score each position relative to its round's mean
    log("summary (per-round normalised, mean over rounds):")
    score = {}
    for q in pos:
        tn = [rw["ten"] / np.mean([x["ten"] for x in rows if x["round"] == rw["round"]]) for rw in rows if rw["pos"] == q]
        tx = [rw["tex"] / np.mean([x["tex"] for x in rows if x["round"] == rw["round"]]) for rw in rows if rw["pos"] == q]
        score[q] = (float(np.mean(tn)), float(np.mean(tx)), float(np.std(tn)), float(np.std(tx)))
        log("  EAF %6d  ten %.3f +- %.3f   tex %.3f +- %.3f" % (q, score[q][0], score[q][2], score[q][1], score[q][3]))
    bt = max(pos, key=lambda q: score[q][0]); bx = max(pos, key=lambda q: score[q][1])
    # parabola vertex on tenengrad
    x = np.array(pos, float); y = np.array([score[q][0] for q in pos])
    c = np.polyfit(x, y, 2); vert = -c[1] / (2 * c[0]) if c[0] < 0 else float("nan")
    log("best: tenengrad %d, texture %d; tenengrad parabola vertex %.0f" % (bt, bx, vert))
    park = a.park if a.park is not None else (bt if bt == bx else int(round(np.median([bt, bx, start]))))
    if bt != bx and a.park is None:
        log("metrics DISAGREE -- parking on median of (ten, tex, start) = %d" % park)
    move_to(p, park); log("PARKED at EAF %d" % focuser_pos(p))
    json.dump(dict(args=vars(a), rows=rows, score={str(k): v for k, v in score.items()}, vertex=vert, park=park), open(os.path.join(a.outdir, "result.json"), "w"), indent=1)
finally:
    try: p.close()
    except Exception: pass
