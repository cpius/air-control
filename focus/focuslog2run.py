#!/usr/bin/env python3
"""Rebuild a dashboard focus-run folder (lib/focusrun.py) from a planetfocus.py log, for runs made before
planetfocus wrote one itself (2026-11-04), and point the dashboard's Focus pane at it.

    python3 -u focus/focuslog2run.py --log ~/ASICAP/telemetry/2026-11-04/planetfocus1.log --tag satfocus1
"""
import argparse, os, re, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from focusrun import FocusRun

ap = argparse.ArgumentParser()
ap.add_argument("--log", required=True, help="planetfocus.py console log (tee'd)")
ap.add_argument("--tag", required=True, help="the run's --tag (names the frames in --imgdir)")
ap.add_argument("--date", default=None, help="YYYY-MM-DD of the run (default: the log's folder name, else today)")
ap.add_argument("--imgdir", default=os.path.expanduser("~/ASICAP/telemetry/planetfocus"), help="planetfocus --outdir")
ap.add_argument("--no-announce", action="store_true", help="write the folder but leave the dashboard on its current run")
a = ap.parse_args()

date = a.date or (os.path.basename(os.path.dirname(os.path.abspath(a.log)))
                  if re.match(r"\d{4}-\d\d-\d\d$", os.path.basename(os.path.dirname(os.path.abspath(a.log)))) else time.strftime("%Y-%m-%d"))
txt = open(a.log, errors="replace").read()
m = re.search(r"metric (size|sharp|moon)\b", txt); metric = m.group(1) if m else "sharp"
m = re.search(r"exposure ([\d.]+)s gain (\d+)", txt); exp_s, gain = (float(m.group(1)), int(m.group(2))) if m else (None, None)
STEP = re.compile(r"^(\d\d:\d\d:\d\d)\s+\+[\d.]+s\s+r(\d+) pos\s+(\d+): (\d+) frames, planet at \(([-\d.]+),([-\d.]+)\) peak (\d+)/255"
                  r" \| size ([\d.]+) px\s+sharp ([\d.]+)\s+moon FWHM (\S+)")
MISS = re.compile(r"^(\d\d:\d\d:\d\d)\s+\+[\d.]+s\s+r(\d+) pos\s+(\d+): no planet in (\d+) frames")

run = FocusRun(a.tag, metric, date=date, fresh=True)
for line in txt.splitlines():
    s = STEP.match(line)
    if s:
        t, r, pos, n, x, y, peak, size, sharp, moon = s.groups()
        mf = float(moon) if moon not in ("-",) and moon.replace(".", "", 1).isdigit() else None
        score = {"sharp": float(sharp), "size": float(size), "moon": mf}[metric]
        run.step("r" + r, int(pos), score, png=os.path.join(a.imgdir, "%s_r%s_p%s.png" % (a.tag, r, pos)),
                 t="%sT%s" % (date, t), peak=int(peak), size_px=float(size), sharp=float(sharp), moon_fwhm_px=mf,
                 frames=int(n), exposure_s=exp_s, gain=gain, planet=[float(x), float(y)])
        continue
    s = MISS.match(line)
    if s:
        t, r, pos, n = s.groups()
        run.step("r" + r, int(pos), None, rejected="no planet", t="%sT%s" % (date, t), frames=int(n), exposure_s=exp_s, gain=gain)

v = re.search(r"vertex by \w+: best sample (\d+), parabola (\d+)", txt)
pk = re.search(r"EAF parked at (\d+)", txt)
edge = re.search(r"is at the edge of the range \((\d+)\)", txt)
run.summary(best_sample=int(v.group(1)) if v else (int(edge.group(1)) if edge else None), vertex=int(v.group(2)) if v else None,
            parked=int(pk.group(1)) if pk else None, at_edge=bool(edge), finished=bool(pk), source_log=os.path.abspath(a.log))
print("%s: %d steps (metric %s), vertex %s, parked %s" % (run.dir, run.n, metric, v.group(2) if v else "-", pk.group(1) if pk else "-"))
if not a.no_announce:
    run.announce()
