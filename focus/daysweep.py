#!/usr/bin/env python3
"""Focuser sweep with frame PAIRS per position (noise-corrected metrics),
auto-exposure that follows sun/cloud, and the roof-edge width. Any pipeline."""
import os, argparse, sys, time, json
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/Users/madsdorup/ASICAP/air-control")
from daypipes import Pipes, log, metrics_pair, edge_width, save_png, move_to, focuser_pos, LO, HI, star_metrics

ap = argparse.ArgumentParser()
ap.add_argument("--page", default="preview", choices=["preview", "focus", "rtmp"])
ap.add_argument("--pos", help="comma list of focuser positions")
ap.add_argument("--lo", type=int); ap.add_argument("--hi", type=int); ap.add_argument("--step", type=int)
ap.add_argument("--rounds", type=int, default=1)
ap.add_argument("--exp", type=float, default=0.002)
ap.add_argument("--gain", type=int, default=0)
ap.add_argument("--bin", type=int, default=2)
ap.add_argument("--auto-exp", action="store_true", help="keep p99 in 18000..50000 (16-bit) / 70..200 (8-bit)")
ap.add_argument("--order", default="interleaved", choices=["interleaved", "linear"])
ap.add_argument("--goto-best", default=None, choices=[None, "ratio", "ten_corr", "edge", "fine_norm"])
ap.add_argument("--park", type=int, default=None)
ap.add_argument("--tag", default="sweep2")
ap.add_argument("--no-png", action="store_true")
ap.add_argument("--edge-rows", default="10,270", help="row window (superpixels) to look for the edge in")
ap.add_argument("--metric", default="scene", choices=["scene", "star"], help="star: HFD/peak of the brightest star with its own auto-exposure")
ap.add_argument("--limits", default=None, help="EAF LO,HI allowed for this run (default: EAF_MIN/EAF_MAX env or daypipes defaults)")
a = ap.parse_args()

import daypipes
if a.limits:
    daypipes.LO, daypipes.HI = [int(x) for x in a.limits.split(",")]
LO, HI = daypipes.LO, daypipes.HI
positions = [int(x) for x in a.pos.split(",")] if a.pos else list(range(a.lo, a.hi + 1, a.step))
for q in positions:
    if not (LO <= q <= HI):
        sys.exit("position %d outside permitted %d..%d" % (q, LO, HI))
srt = sorted(positions)
order = (srt[::2] + srt[1::2][::-1]) if (a.order == "interleaved" and len(positions) > 2) else list(positions)
er = tuple(int(x) for x in a.edge_rows.split(","))

p = Pipes()
rows = []
outdir = "frames/%s_%s" % (a.tag, a.page)
os.makedirs(outdir, exist_ok=True)
start = focuser_pos(p)
exp = a.exp
log("EAF at %d; %s page, exp %g gain %d bin %d auto=%s; positions %s, %d round(s), order %s" % (
    start, a.page, exp, a.gain, a.bin, a.auto_exp, positions, a.rounds, order))

def set_exp(e):
    global exp
    exp = e
    p.c("set_control_value", ["Exposure", int(round(e * 1_000_000))])
    p.exp = e
    log("  exposure -> %.4fs" % e)

try:
    p.setup(a.page, exp, a.gain, a.bin)
    for r in range(a.rounds):
        seq = order if r % 2 == 0 else order[::-1]
        for pos in seq:
            got = move_to(p, pos)
            p.grab()                                   # discard: may predate the move
            while True:
                t = time.time()
                img1, w, h, i1 = p.grab()
                img2, w, h, i2 = p.grab()
                dt = time.time() - t
                depth = i1["depth"]
                p99 = float(np.percentile(img2, 99))
                if a.auto_exp and a.metric == "scene":
                    lo_t, hi_t = (18000, 50000) if depth == 16 else (70, 200)
                    if p99 < lo_t and exp < 0.05:
                        set_exp(min(exp * 2, 0.05)); p.grab(); continue
                    if p99 > hi_t and exp > 0.00006:
                        set_exp(max(exp / 2, 0.00006)); p.grab(); continue
                if a.metric == "star" and a.auto_exp:
                    sm1, sm2 = star_metrics(img1), star_metrics(img2)
                    if sm1 and sm2:
                        pk = max(sm1["peak"], sm2["peak"]); sat = 60000 if depth == 16 else 230
                        if pk > sat * 0.9 and exp > 0.0002:
                            set_exp(max(exp / 2, 0.0002)); p.grab(); continue
                        if pk < sat * 0.12 and exp < 2.0:
                            set_exp(min(exp * 2, 2.0)); p.grab(); continue
                break
            if a.metric == "star":
                sm1, sm2 = star_metrics(img1), star_metrics(img2)
                m = dict(mean=0.0, fine=0.0, mid=0.0, noise=0.0, ratio=0.0, ten_corr=0.0, fine_norm=0.0, p99=0.0, max=0.0)
                if sm1 and sm2:
                    m.update(hfd=float(np.median([sm1["hfd"], sm2["hfd"]])), peak=float(np.mean([sm1["peak"], sm2["peak"]])),
                             flux=float(np.mean([sm1["flux"], sm2["flux"]])), diam=float(np.median([sm1["diam"], sm2["diam"]])),
                             sx=sm1["x"], sy=sm1["y"])
                    m["ten_corr"] = m["peak"] / max(m["flux"], 1) * 1e4     # concentration: peak / flux
                    m["fine_norm"] = 1000.0 / max(m["hfd"], 0.1)
                else:
                    m.update(hfd=float("nan"), peak=0.0, flux=0.0, diam=float("nan"), sx=-1, sy=-1)
                ew, erow, eamp = m["hfd"], int(m["sy"]), m["peak"]
            else:
                m = metrics_pair(img1, img2)
                ew, erow, eamp = edge_width((img1 + img2) / 2, rows=er)
            row = dict(round=r, pos=got, w=w, h=h, depth=depth, dt_pair=dt, exp=exp,
                       fresh=(i1["fresh"], i2["fresh"]), event=(i1.get("event"), i2.get("event")),
                       edge=ew, edge_row=erow, edge_amp=eamp, **{k: float(v) for k, v in m.items()})
            rows.append(row)
            if not a.no_png:
                fn = "%s/r%d_p%d.png" % (outdir, r, got)
                save_png(img1, fn, shrink=2)
                np.save(fn.replace(".png", ".npy"), img1.astype(np.uint16) if depth == 16 else img1.astype(np.uint8))
            if a.metric == "star":
                log("r%d pos %6d: %dx%d %db pair %.1fs exp %.4f fresh=%s | star x=%s y=%s peak %6.0f flux %9.0f | HFD %5.1f px  diam %5.1f px  conc %.3f" % (
                    r, got, w, h, depth, dt, exp, row["fresh"], m["sx"], m["sy"], m["peak"], m["flux"], m["hfd"], m["diam"], m["ten_corr"]))
            else:
              log("r%d pos %6d: %dx%d %db pair %.1fs exp %.4f fresh=%s ev=%s | mean %6.0f p99 %6.0f noise %5.1f | ratio %.4f fine_n %6.2f ten %7.3f | edge %s @row %s amp %s" % (
                r, got, w, h, depth, dt, exp, row["fresh"], row["event"], m["mean"], m["p99"], m["noise"],
                m["ratio"], m["fine_norm"], m["ten_corr"],
                "%.1f" % ew if ew else "-", erow, "%.0f" % eamp if eamp else "-"))
    log("=== summary (median over rounds) ===")
    byp = {}
    for row in rows:
        byp.setdefault(row["pos"], []).append(row)
    summ = []
    for pos in sorted(byp):
        rs = byp[pos]
        med = lambda k: float(np.median([x[k] for x in rs if x[k] is not None])) if any(x[k] is not None for x in rs) else float("nan")
        s = dict(pos=pos, n=len(rs), ratio=med("ratio"), ten_corr=med("ten_corr"), fine_norm=med("fine_norm"), edge=med("edge"), mean=med("mean"))
        summ.append(s)
        log("  %6d n=%d  ratio %.4f  ten %7.3f  fine_n %6.2f  edge %5.1f  mean %6.0f" % (
            pos, s["n"], s["ratio"], s["ten_corr"], s["fine_norm"], s["edge"], s["mean"]))
    best = {}
    for k in ("ratio", "ten_corr", "fine_norm"):
        best[k] = max(summ, key=lambda s: s[k])["pos"]
    ok = [s for s in summ if not np.isnan(s["edge"])]
    if ok:
        best["edge"] = min(ok, key=lambda s: s["edge"])["pos"]
    log("best by metric: %s" % best)
    json.dump(dict(args=vars(a), rows=rows, summary=summ, best=best), open("%s/result.json" % outdir, "w"), indent=1, default=str)
    park = a.park
    if a.goto_best and a.goto_best in best:
        park = best[a.goto_best]
    if park is None:
        park = start
    move_to(p, park)
    log("EAF parked at %d" % focuser_pos(p))
finally:
    p.close()
    log("closed")
