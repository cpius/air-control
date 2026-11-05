#!/usr/bin/env python3
"""Follow a cliploop log and print one line per finished clip with its tip-tilt seeing
(the tiltseeing.py estimate on that clip's hold CSV). An upper bound: mount shake adds in.

    python3 -u planetary/clipseeing.py --log ~/ASICAP/telemetry/2026-11-04/cliploop_sat2.log --scale 0.162
"""
import argparse, csv, math, os, re, sys, time
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("--log", required=True)
ap.add_argument("--scale", type=float, default=0.162, help="bin-1 arcsec/px of the clips")
ap.add_argument("--after", default="", help="HH:MM:SS -- ignore clips finished before this (already reported)")
a = ap.parse_args()
LAM, D, R2A = 550e-9, 0.203, 206265.0

def seeing(path):
    rows = [r for r in csv.DictReader(open(path)) if r.get("planet") == "1" and r.get("x")]
    if len(rows) < 30: return None
    t = np.array([float(r["t_s"]) for r in rows]); x = np.array([float(r["x"]) for r in rows]); y = np.array([float(r["y"]) for r in rows])
    ok = (np.diff(t) > 0) & (np.diff(t) < 2.0); o = []        # 2026-11-04 clip 5: frames ~1.05 s apart left no pairs under 1 s
    if ok.sum() < 20: return None
    for v in (x, y):
        d = np.diff(v)[ok]; o.append(1.4826 * np.median(np.abs(d - np.median(d))) / math.sqrt(2) * a.scale)
    sig = math.sqrt((o[0] ** 2 + o[1] ** 2) / 2) / R2A
    if not sig > 0: return None
    r0 = D * (0.17 * (LAM / D) ** 2 / sig ** 2) ** (3 / 5)
    return o[0], o[1], 0.98 * LAM / r0 * R2A

def word(s):
    return "excellent" if s < 0.8 else "good" if s < 1.2 else "fair" if s < 2.0 else "poor"

clip, total, pos = "?", "?", 0
while not os.path.exists(a.log): time.sleep(1)
with open(a.log) as fh:
    while True:
        line = fh.readline()
        if not line:
            time.sleep(1); continue
        m = re.search(r"clip (\d+) of (\d+)", line)
        if m: clip, total = m.group(1), m.group(2); continue
        if "RECORDED" in line and line[:8] >= a.after:
            m = re.search(r"per frame: (\S+_hold\.csv)", line); f = re.search(r"planet in (\d+) of (\d+)", line)
            s = None
            for _ in range(5):                    # the CSV may still be flushing when RECORDED is logged (clip 3, 22:03)
                s = seeing(m.group(1)) if m and os.path.exists(m.group(1)) else None
                if s: break
                time.sleep(2)
            if s:
                print("clip %s/%s done %s  seeing %.2f\" (%s)  RA %.2f\" Dec %.2f\"  planet %s/%s checks" % (
                    clip, total, line[:5], s[2], word(s[2]), s[0], s[1], f.group(1) if f else "?", f.group(2) if f else "?"), flush=True)
            else:
                print("clip %s/%s done %s  seeing: no hold data" % (clip, total, line[:5]), flush=True)
        elif any(k in line for k in ("did not record", "stopping", "loop finished", "Traceback", "ABORT", "refus")):
            print("LOOP: " + line.strip()[:160], flush=True)
            if "loop finished" in line or "stopping" in line: sys.exit(0)
