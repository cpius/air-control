#!/usr/bin/env python3
"""From "Saturn's big disc found at a register offset" to long recorded clips, unattended.

Stages (each logs to the session log; --start-at resumes):
  shrink   EAF to --eaf-small so the disc fits the frame
  find     mini-search around the found offset (blob only)
  centre   centre.py: measures the Jacobian, syncs the register on Saturn's ephemeris position
  drift    blobdrift.py: 60 s of centroids -> arcmin/min, for the re-acquire prediction
  focus    starfocus.py around --focus (hot-pixel safe, holds the planet)
  adc      adccheck.py: red-minus-blue centroid offset, informational
  record   loop: saturnwatch.py (re-acquire + cloud gate) -> satvideo.py --seconds N, until --clips or --until HH:MM

    ASIAIR_HOST=192.168.1.35 EAF_MIN=15000 EAF_MAX=98000 python3 -u saturn_go.py --pred-dra -90 --pred-ddec -150
"""
import argparse, datetime as dt, json, os, re, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("--ra", type=float, default=0.7167); ap.add_argument("--dec", type=float, default=1.991)
ap.add_argument("--pred-dra", type=float, default=0.0); ap.add_argument("--pred-ddec", type=float, default=0.0)
ap.add_argument("--eaf-small", type=int, default=50000); ap.add_argument("--focus", type=int, default=60900)
ap.add_argument("--start-at", default="shrink", choices=["shrink", "find", "centre", "drift", "focus", "adc", "record"])
ap.add_argument("--jacobian", default=None, help="bin-2 px/arcmin if already measured (skips the calibration moves)")
ap.add_argument("--drift", default="0,0", help="arcmin/min RA,Dec if already measured")
ap.add_argument("--seconds", type=float, default=300); ap.add_argument("--clips", type=int, default=20); ap.add_argument("--until", default="03:20")
ap.add_argument("--exp-ms", type=float, default=40); ap.add_argument("--gain", type=int, default=350); ap.add_argument("--roi", type=int, default=1000)
a = ap.parse_args()
ENV = dict(os.environ, ASIAIR_HOST=os.environ.get("ASIAIR_HOST", "192.168.1.35"), EAF_MIN=os.environ.get("EAF_MIN", "15000"), EAF_MAX=os.environ.get("EAF_MAX", "98000"))
LOG = os.path.join(HERE, "..", "telemetry", "2026-09-16_session.log")
def log(s):
    line = f"{time.strftime('%H:%M:%S')} GO {s}"; print(line, flush=True)
    with open(LOG, "a") as f: f.write(line + "\n")

def run(cmd, timeout=1800, keep=None):
    """Run a tool, stream its lines to our stdout, return the full text."""
    log("$ " + " ".join(cmd))
    p = subprocess.Popen(cmd, cwd=HERE, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = []; t0 = time.time()
    for line in p.stdout:
        line = line.rstrip("\n"); out.append(line)
        if "warning" in line.lower() or "receive " in line or "heartbeat/status" in line or "info  rpc" in line or "info  image" in line or "info  mount" in line or "drain" in line:
            continue
        if keep is None or any(k in line for k in keep):
            print("   " + line[:200], flush=True)
        if time.time() - t0 > timeout:
            p.kill(); log("  (killed after timeout)"); break
    p.wait(); return "\n".join(out)

def eaf_to(pos):
    from air_rpc import Air
    cam = Air(ENV["ASIAIR_HOST"], 4700, key=os.path.join(HERE, "embedded_key.pem"))
    c = lambda m, p=None: cam.call(m, p or [], timeout=20).get("result")
    log(f"EAF {c('get_focuser_position')} -> {pos}: {c('move_focuser', [int(pos)])}")
    t0 = time.time()
    while time.time() - t0 < 240:
        st = c("get_focuser_state")
        if isinstance(st, dict) and st.get("state") == "idle": break
        time.sleep(3)
    log(f"EAF at {c('get_focuser_position')}")
    cam.close()

def register():
    from air_rpc import Air
    i = Air(ENV["ASIAIR_HOST"], 4400).call("scope_get_info", [], timeout=10)["result"]
    return i["RA"], i["Dec"], i.get("pier_side")

stages = ["shrink", "find", "centre", "drift", "focus", "adc", "record"]
todo = stages[stages.index(a.start_at):]
pred = [a.pred_dra, a.pred_ddec]; J2 = a.jacobian; drift = [float(v) for v in a.drift.split(",")]; t_sync = None

if "shrink" in todo:
    eaf_to(a.eaf_small)

if "find" in todo:
    out = run([sys.executable, "-u", "search.py", "--ra", str(a.ra), "--dec", str(a.dec), "--name", "Saturn", "--pred-dra", str(pred[0]), "--pred-ddec", str(pred[1]),
               "--search-ra", "16", "--step-ra", "8", "--search-dec", "10", "--step-dec", "5", "--exp", "0.5", "--gain", "300", "--nsig", "5", "--min-area", "2000", "--no-jump"],
              keep=["FOUND", "RESULT", "nothing", "/"])
    m = re.search(r"RESULT pred_dra=([-\d.]+) pred_ddec=([-\d.]+)", out)
    if not m:
        log("find: Saturn NOT found in the mini-search -- stopping"); sys.exit(2)
    pred = [float(m.group(1)), float(m.group(2))]; log(f"find: Saturn at offset {pred}")

if "centre" in todo:
    cmd = [sys.executable, "-u", "centre.py", "--ra", str(a.ra), "--dec", str(a.dec), "--exp", "0.5", "--gain", "300", "--tol-px", "40", "--cal-arcmin", "1.0"]
    if J2: cmd += ["--jacobian", J2]
    out = run(cmd, keep=["star at", "Jacobian", "iter", "register", "sync", "lost"])
    m = re.search(r"Jacobian px/arcmin: \[\[([-\d.]+), ([-\d.]+)\], \[([-\d.]+), ([-\d.]+)\]\]", out)
    if m: J2 = ",".join(m.groups())
    if not J2:
        log("centre: no Jacobian -- stopping"); sys.exit(3)
    t_sync = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ra, de, pier = register(); log(f"centre: J2 (bin 2, pier {pier}) = {J2}; register {ra:.4f} {de:.4f}; synced at {t_sync}")

if "drift" in todo:
    out = run([sys.executable, "-u", "blobdrift.py", "--seconds", "60", "--every", "10", "--exp", "0.5", "--gain", "300", "--jacobian", J2], keep=["drift", "blob"])
    m = re.search(r"drift in sky terms: RA ([-+\d.]+)'/min  Dec ([-+\d.]+)'/min", out)
    if m: drift = [float(m.group(1)), float(m.group(2))]
    log(f"drift: {drift} arcmin/min")

if "focus" in todo:
    eaf_to(a.focus)
    out = run([sys.executable, "-u", "starfocus.py", "--lo", str(a.focus - 1500), "--hi", str(a.focus + 1500), "--step", "300", "--exp", "0.1", "--gain", "150",
               "--jacobian", J2, "--recentre-px", "200"], keep=["EAF", "===", "held", "NO STAR", "parked"])
    m = re.search(r"parabola vertex (\d+)", out)
    if m: a.focus = int(m.group(1))
    log(f"focus: parked at {a.focus}")

if "adc" in todo:
    run([sys.executable, "-u", "adccheck.py", "--page", "preview", "--exp", "0.05", "--gain", "100", "--frames", "5", "--arcsec-per-px", "0.113"], keep=["R-B", "frame"], timeout=180)

if "record" in todo:
    J2v = [float(v) for v in J2.split(",")]
    ra, de, pier = register()
    j2_east = J2v if pier == "east" else [-v for v in J2v]           # cloudcheck wants the pier-east form
    j1 = [2 * v for v in J2v]                                          # satvideo: bin 1, current pier side
    until = dt.datetime.strptime(a.until, "%H:%M").time()
    for i in range(a.clips):
        now = dt.datetime.now()
        if now.time() > until and now.time() < dt.time(12, 0):
            log(f"record: past --until {a.until}, stopping"); break
        log(f"record: clip {i+1}/{a.clips}, {a.seconds:.0f} s")
        run([sys.executable, "-u", "saturnwatch.py", "--ra", str(a.ra), "--dec", str(a.dec), "--t-sync", t_sync or dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "--drift-ra", str(drift[0]), "--drift-dec", str(drift[1]), "--jacobian2", ",".join(f"{v:.1f}" for v in j2_east), "--jacobian1", ",".join(f"{v:.1f}" for v in j1),
             "--seconds", str(a.seconds), "--roi", str(a.roi), "--exp-ms", str(a.exp_ms), "--gain", str(a.gain), "--max-dim-exp", "90", "--max-dim-gain", "450",
             "--search-ra", "10", "--search-dec", "8", "--cycles", "1", "--every", "120", "--log", os.path.join(HERE, "..", "telemetry", "2026-09-16_saturnwatch.log"),
             "--state", os.path.join(HERE, "..", "telemetry", "saturnwatch_state_2026-09-16.json")],
            keep=["VERDICT", "RECORDED", "EXIT", "ERROR", "ABORT", "exposure", "hold", "centred", "sync", "lost", "search", "Traceback"], timeout=a.seconds + 900)
    log("record: done")
