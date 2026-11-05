#!/usr/bin/env python3
"""Copy a night's AVIs off the Air (USB-C volume or SMB mount) best-seeing first, keeping --margin-gb free.
Seeing per clip = tip-tilt from satvideo's hold CSV (telemetry/video/<local HHMMSS>_hold.csv; the AVI name is UTC).
Verifies each copy by size + 4 MB head/tail md5. Deletes nothing on the Air.

    python3 -u planetary/pullbyseeing.py --src /Volumes/asiair/Video --date 2026-11-04 --dest ~/ASICAP/Saturn/2026-11-04
"""
import argparse, csv, datetime as dt, glob, hashlib, math, os, shutil, subprocess, sys, time
import numpy as np
ap = argparse.ArgumentParser()
ap.add_argument("--src", required=True); ap.add_argument("--date", required=True); ap.add_argument("--dest", required=True)
ap.add_argument("--margin-gb", type=float, default=5.0); ap.add_argument("--scale", type=float, default=0.162)
ap.add_argument("--utc-offset-h", type=float, default=1.0, help="local - UTC (CET = 1)")
ap.add_argument("--dry-run", action="store_true")
a = ap.parse_args()
LAM, D, R2A = 550e-9, 0.203, 206265.0
VID = os.path.expanduser("~/ASICAP/telemetry/video")

def seeing(path):
    rows = [r for r in csv.DictReader(open(path)) if r.get("planet") == "1" and r.get("x")]
    if len(rows) < 30: return None
    t = np.array([float(r["t_s"]) for r in rows]); x = np.array([float(r["x"]) for r in rows]); y = np.array([float(r["y"]) for r in rows])
    ok = (np.diff(t) > 0) & (np.diff(t) < 2.0)
    if ok.sum() < 20: return None
    o = [1.4826 * np.median(np.abs(np.diff(v)[ok] - np.median(np.diff(v)[ok]))) / math.sqrt(2) * a.scale for v in (x, y)]
    sig = math.sqrt((o[0] ** 2 + o[1] ** 2) / 2) / R2A
    return 0.98 * LAM / (D * (0.17 * (LAM / D) ** 2 / sig ** 2) ** 0.6) * R2A if sig > 0 else None

holds = {}
for p in glob.glob(os.path.join(VID, "*_hold.csv")):
    if time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(p) - 12 * 3600)) != a.date: continue
    b = os.path.basename(p)[:6]; holds[int(b[:2]) * 3600 + int(b[2:4]) * 60 + int(b[4:])] = p
clips = []
for f in sorted(glob.glob(os.path.join(a.src, a.date + "-*.avi"))):
    hh = os.path.basename(f)[11:17]; tu = int(hh[:2]) * 3600 + int(hh[2:4]) * 60 + int(hh[4:]); tl = (tu + int(a.utc_offset_h * 3600)) % 86400
    near = min(holds, key=lambda k: abs(k - tl)) if holds else None
    hp = holds[near] if near is not None and abs(near - tl) < 40 else None
    s = seeing(hp) if hp else None
    clips.append(dict(f=f, size=os.path.getsize(f), see=s, hold=hp))
clips.sort(key=lambda c: (c["see"] is None, c["see"] if c["see"] else 9, -c["size"]))
print("%d clips, %.1f GB; ranked best seeing first:" % (len(clips), sum(c["size"] for c in clips) / 1e9))
for c in clips:
    print("  %s  %5.2f GB  seeing %s" % (os.path.basename(c["f"]), c["size"] / 1e9, ("%.2f\"" % c["see"]) if c["see"] else "  -  "))
if a.dry_run: sys.exit(0)
os.makedirs(a.dest, exist_ok=True)
def free_gb(): return shutil.disk_usage(a.dest).free / 1e9
def ht(p):
    h = hashlib.md5(); n = os.path.getsize(p)
    with open(p, "rb") as fh:
        h.update(fh.read(4 << 20)); fh.seek(max(0, n - (4 << 20))); h.update(fh.read(4 << 20))
    return h.hexdigest()
done, skipped = [], []
for c in clips:
    f = c["f"]; b = os.path.basename(f); out = os.path.join(a.dest, b)
    for side in (f[:-4] + ".avi.txt", f[:-4] + "_thn.jpg"):
        if os.path.exists(side) and not os.path.exists(os.path.join(a.dest, os.path.basename(side))): shutil.copy2(side, a.dest)
    if os.path.exists(out) and os.path.getsize(out) == c["size"]:
        done.append(c); print("%s have %s" % (time.strftime("%H:%M:%S"), b), flush=True); continue
    if free_gb() - c["size"] / 1e9 < a.margin_gb:
        skipped.append(c); print("%s SKIP %s (%.1f GB) -- would leave < %.0f GB free" % (time.strftime("%H:%M:%S"), b, c["size"] / 1e9, a.margin_gb), flush=True); continue
    t0 = time.time(); part = out + ".part"
    with open(f, "rb") as src, open(part, "wb") as dst:
        got, last = 0, time.time()
        while True:
            buf = src.read(8 << 20)
            if not buf: break
            dst.write(buf); got += len(buf)
            if time.time() - last > 10:
                print("%s   %s  %d/%d MB  %.0f MB/s" % (time.strftime("%H:%M:%S"), b, got >> 20, c["size"] >> 20, got / 1e6 / (time.time() - t0)), flush=True); last = time.time()
    os.rename(part, out)
    ok = os.path.getsize(out) == c["size"] and ht(out) == ht(f)
    print("%s %s %s  %.2f GB in %.0f s  seeing %s" % (time.strftime("%H:%M:%S"), "OK  " if ok else "BAD ", b, c["size"] / 1e9, time.time() - t0, ("%.2f\"" % c["see"]) if c["see"] else "-"), flush=True)
    done.append(c)
print("=== copied %d clips (%.1f GB); skipped %d (%.1f GB) -- still on the Air; free now %.1f GB" % (
    len(done), sum(c["size"] for c in done) / 1e9, len(skipped), sum(c["size"] for c in skipped) / 1e9, free_gb()), flush=True)
