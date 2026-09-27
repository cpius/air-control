#!/usr/bin/env python3
"""Wait for a gap in the cloud and record the planet when it comes.

Each cycle: predict where the planet has drifted to since the last sync
(the register keeps reading the target while the tube drifts ~1.3'/min with
tonight's polar alignment), re-acquire and centre it with cloudcheck.py
(which syncs the register on success), then hand over to satvideo.py, whose
exposure test doubles as the transparency test: if the planet needs more
than --max-dim-exp ms or more gain than --max-dim-gain to reach the target
peak, it is behind cloud and nothing is recorded. Exit 0 once a clip is on
the Air. The sync time is kept in --state so the drift prediction survives
between runs.

    ASIAIR_HOST=192.168.1.35 python3 -u saturnwatch.py --cycles 1 --every 300
"""
import argparse, json, os, subprocess, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("--ra", type=float, default=0.8545); ap.add_argument("--dec", type=float, default=2.575)
ap.add_argument("--state", default="/Users/madsdorup/ASICAP/telemetry/saturnwatch_state.json")
ap.add_argument("--t-sync", default="2026-09-15 00:11:21", help="when the register was last synced on the planet (used if no state file)")
ap.add_argument("--drift-ra", type=float, default=0.05, help="arcmin/min the planet moves relative to the register")
ap.add_argument("--drift-dec", type=float, default=-1.3)
ap.add_argument("--every", type=float, default=300); ap.add_argument("--cycles", type=int, default=1)
ap.add_argument("--jacobian2", default="-153.8,-107.0,115.7,-134.3", help="cloudcheck Jacobian (pier-east form, negated automatically)")
ap.add_argument("--jacobian1", default="307.6,214.0,-231.4,268.6", help="satvideo Jacobian, bin 1, current pier side")
ap.add_argument("--seconds", type=float, default=30); ap.add_argument("--roi", type=int, default=1000)
ap.add_argument("--exp-ms", type=float, default=40); ap.add_argument("--gain", type=int, default=350)
ap.add_argument("--max-dim-exp", type=float, default=60); ap.add_argument("--max-dim-gain", type=int, default=400)
ap.add_argument("--log", default="/Users/madsdorup/ASICAP/telemetry/2026-09-15_saturnwatch.log")
ap.add_argument("--search-ra", type=float, default=8.0); ap.add_argument("--search-dec", type=float, default=9.0)
a = ap.parse_args()
HERE = os.path.dirname(os.path.abspath(__file__))
ENV = dict(os.environ, ASIAIR_HOST=os.environ.get("ASIAIR_HOST", "192.168.1.35"), EAF_MIN="500", EAF_MAX="99000")
KEEP = ("VERDICT", "centred", "scope_sync", "EXIT", "ERROR", "sky @", "exposure test", "ABORT", "RECORDED", "hold:",
        "correction", "exposure used", "Traceback", "lost the planet", "search:")

def log(msg):
    line = time.strftime("%H:%M:%S") + "  " + msg
    print(line, flush=True)
    with open(a.log, "a") as f:
        f.write(line + "\n")

def run(cmd, timeout):
    out = []
    p = subprocess.Popen(cmd, cwd=HERE, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    t0 = time.time()
    for line in p.stdout:
        line = line.rstrip()
        out.append(line)
        if any(k in line for k in KEEP):
            log("    " + line)
        if time.time() - t0 > timeout:
            p.kill(); log("    killed %s after %d s" % (cmd[2], timeout)); break
    p.wait()
    return p.returncode, "\n".join(out)

if os.path.exists(a.state):
    state = json.load(open(a.state))
else:
    state = {"t_sync": time.mktime(time.strptime(a.t_sync, "%Y-%m-%d %H:%M:%S"))}
for cyc in range(1, a.cycles + 1):
    dt = (time.time() - state["t_sync"]) / 60.0
    pred = (a.drift_ra * dt, a.drift_dec * dt)
    log("cycle %d: %.1f min since the last sync -> commanding RA %+.1f' Dec %+.1f' from nominal" % (cyc, dt, pred[0], pred[1]))
    rc, out = run(["python3", "-u", "cloudcheck.py", "--ra", str(a.ra), "--dec", str(a.dec), "--name", "Saturn",
                   "--pred-dra", "%.2f" % pred[0], "--pred-ddec", "%.2f" % pred[1], "--jacobian", a.jacobian2,
                   "--planet-peak", "300", "--planet-area", "200", "--cloud-rate", "300", "--no-solve",
                   "--search-ra", str(a.search_ra), "--search-dec", str(a.search_dec), "--step-ra", "8", "--step-dec", "4.5", "--max-checks", "1"], 420)
    if "scope_sync to Saturn ephemeris position -> 0" in out:
        state["t_sync"] = time.time()
        json.dump(state, open(a.state, "w"))
        log("  centred and synced -> recorder")
        rc2, out2 = run(["python3", "-u", "satvideo.py", "--seconds", str(a.seconds), "--roi", str(a.roi),
                         "--exp-ms", str(a.exp_ms), "--gain", str(a.gain), "--jacobian", a.jacobian1, "--deadband", "60",
                         "--max-dim-exp", str(a.max_dim_exp), "--max-dim-gain", str(a.max_dim_gain)], 300)
        if "RECORDED" in out2:
            log("RECORDED -- a clip is on the Air; stopping the watch")
            sys.exit(0)
        log("  not recorded: %s" % ("too dim = cloud" if "too dim" in out2 else "planet not in the window"))
    else:
        log("  Saturn not centred this cycle: %s" % ("COVERED" if "VERDICT COVERED" in out else "not found / centring lost it"))
    log("  waiting %.0f s" % a.every)
    t0 = time.time()
    while time.time() - t0 < a.every:
        time.sleep(min(30, max(0.1, a.every - (time.time() - t0))))
        left = a.every - (time.time() - t0)
        if left > 1:
            log("  ... %.0f s to the next check" % left)
sys.exit(2)
