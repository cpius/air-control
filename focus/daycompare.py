#!/usr/bin/env python3
"""Collate sweep2 result.json files into one comparison table."""
import os, json, sys, numpy as np
from scipy.optimize import curve_fit

def hyper(x, x0, w0, s): return np.sqrt(w0**2 + (s*(x-x0))**2)

def analyse(path):
    res = json.load(open(path)); rows = res["rows"]; a = res["args"]
    out = dict(tag=a["tag"], page=a["page"], bin=a["bin"], n=len(rows))
    out["geom"] = "%dx%d %d-bit" % (rows[0]["w"], rows[0]["h"], rows[0]["depth"])
    out["pair_s"] = float(np.median([r["dt_pair"] for r in rows]))
    out["fresh"] = sum(all(r["fresh"]) for r in rows)
    out["event"] = sum(all(bool(e) for e in r["event"]) for r in rows)
    out["exp"] = sorted(set(round(r["exp"], 5) for r in rows))
    xs = np.array([r["pos"] for r in rows if r["edge"]]); ws = np.array([r["edge"] for r in rows if r["edge"]])
    try:
        p, cov = curve_fit(hyper, xs, ws, p0=[xs[np.argmin(ws)], ws.min(), 0.01]); e = np.sqrt(np.diag(cov))
        out["edge_vertex"] = "%.0f +/- %.0f" % (p[0], e[0]); out["edge_floor"] = "%.1f" % p[1]
        # sensor px per superpixel: 4 at bin 2 (preview/focus), 2 on the bin-1 rtmp window
        spx = 2 if (a["page"] == "rtmp" or rows[0]["depth"] == 8) else (4 if a["bin"] == 2 else 2)
        out["um_per_step"] = "%.2f" % (abs(p[2]) * spx * 2.9 * 10)
    except Exception as ex:
        out["edge_vertex"] = "fit failed"; out["edge_floor"] = "-"; out["um_per_step"] = "-"
    for k in ("ratio", "ten_corr", "fine_norm"):
        xs = np.array([r["pos"] for r in rows]); ys = np.array([r[k] for r in rows])
        top = sorted(set(xs[np.argsort(ys)[::-1]][:8])); sel = np.isin(xs, top) & (ys > 0)
        v = "-"
        if sel.sum() >= 3:
            c = np.polyfit(xs[sel], np.log(ys[sel]), 2)
            if c[0] < 0: v = "%.0f" % (-c[1] / (2 * c[0]))
        out[k + "_vertex"] = v
        # peak-to-floor contrast of the curve: median of best position over median of worst
        byp = {}
        for r in rows: byp.setdefault(r["pos"], []).append(r[k])
        meds = {p_: float(np.median(v_)) for p_, v_ in byp.items()}
        out[k + "_range"] = "%.1fx" % (max(meds.values()) / max(min(meds.values()), 1e-9))
    # round-to-round repeatability at the same position
    byp = {}
    for r in rows: byp.setdefault(r["pos"], []).append(r["ten_corr"])
    rep = [abs(v[0] - v[1]) / max(np.mean(v), 1e-9) for v in byp.values() if len(v) == 2]
    out["ten_repeat"] = "%.0f%%" % (100 * np.median(rep)) if rep else "-"
    return out

keys = ["tag", "page", "bin", "geom", "pair_s", "fresh", "event", "exp", "edge_vertex", "edge_floor", "um_per_step",
        "ten_corr_vertex", "fine_norm_vertex", "ratio_vertex", "ten_corr_range", "fine_norm_range", "ratio_range", "ten_repeat"]
rows = [analyse(p) for p in sys.argv[1:]]
for k in keys:
    print("%-17s " % k + "  ".join("%-22s" % str(r.get(k)) for r in rows))
