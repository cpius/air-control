#!/usr/bin/env python3
"""Guide log, like the ASIAIR app's: hold a 4400 connection while the Air guides and keep every guide step.

The Air pushes a PHD2-style `GuideStep` event (RA/Dec error, pulse, star SNR/mass) to every connected client, once
per guide frame -- but only while something is connected, and none of our tools stays connected while it guides.
This does, so:

  * every event lands in the dashboard's event log (air_rpc's recorder tap) -> the dashboard's Guiding pane;
  * a PHD2-format log is written to ~/ASICAP/telemetry/<date>/PHD2_GuideLog_<date>_<time>.txt, one
    "Guiding Begins/Ends" section per guiding run, readable by PHD2 Log Viewer like the app's own logs.

Read-only: it calls only get_* methods (and get_app_state every --poll s, which also keeps 4400 from hanging up the
idle socket). It never starts, stops or tunes guiding.

    ASIAIR_HOST=192.168.1.40 python3 -u rig/guidelog.py --guide-scale 9.17

The Air reports RADistanceRaw/DECDistanceRaw in ARCSEC (|dx,dy| px x 9.17 matches them, and the pulse lengths
match arcsec / guide rate); the PHD2 columns are pixels, so they are divided by --guide-scale (or the Air's
get_pixel_scale when it answers). Heartbeat every --heartbeat s with the running RMS.
"""
import argparse, datetime as dt, math, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from air_rpc import Air

ap = argparse.ArgumentParser()
ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"), required="ASIAIR_HOST" not in os.environ)
ap.add_argument("--guide-scale", type=float, default=9.17, help="guide arcsec/px if the Air does not say (FMA180 + 220MM bin 2)")
ap.add_argument("--poll", type=float, default=5.0, help="seconds between get_app_state keepalives")
ap.add_argument("--heartbeat", type=float, default=30.0)
ap.add_argument("--rms-window", type=int, default=50, help="guide steps in the running RMS")
ap.add_argument("--outdir", default=None, help="default ~/ASICAP/telemetry/<local date>")
a = ap.parse_args()

T0 = time.time()
def log(s): print("%s +%6.0fs  %s" % (time.strftime("%H:%M:%S"), time.time() - T0, s), flush=True)
day = (dt.datetime.now() - dt.timedelta(hours=12)).strftime("%Y-%m-%d")      # a session belongs to its evening
outdir = os.path.expanduser(a.outdir or "~/ASICAP/telemetry/%s" % day); os.makedirs(outdir, exist_ok=True)
path = os.path.join(outdir, "PHD2_GuideLog_%s.txt" % dt.datetime.now().strftime("%Y-%m-%d_%H%M%S"))
f = open(path, "a")
def w(line): f.write(line + "\n"); f.flush()
w("PHD2 version 2.6.13 (ASIAIR via guidelog.py), Log version 2.5. Log enabled at %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
log("writing %s" % path)

COLS = "Frame,Time,mount,dx,dy,RARawDistance,DECRawDistance,RAGuideDistance,DECGuideDistance,RADuration,RADirection,DECDuration,DECDirection,XStep,YStep,StarMass,SNR,ErrorCode"
in_section = False; last_frame = None; scale = a.guide_scale; steps = []; n_total = 0; state = "?"

def res(m, method, params=None):
    try:
        r = m.call(method, params or [], timeout=8)
        return r.get("result") if isinstance(r, dict) and r.get("code", 0) == 0 else None
    except Exception:
        return None

def begin(m, why):
    global in_section, scale
    if in_section: end("restarted")
    ps = res(m, "get_pixel_scale")
    if isinstance(ps, (int, float)) and ps > 0: scale = float(ps)
    exp = res(m, "get_exposure")
    ap_ = {(ax, k): res(m, "get_algo_param", [ax, k]) for ax in ("ra", "dec") for k in ("aggression", "minMove", "hysteresis", "fastSwitch")}
    si = res(m, "scope_get_info") or {}
    rate = res(m, "scope_get_guide_rate")
    w("")
    w("Guiding Begins at %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    w("Dither = both axes, Dither scale = 1.000, Image noise reduction = none, Guide-frame time lapse = 0, Server enabled")
    w("Pixel scale = %.2f arc-sec/px, Binning = 2, Focal length = %.0f mm" % (scale, 206.265 * 2.9 * 2 / scale))
    w("Search region = 15 px, Star mass tolerance = 50.0%")
    w("Equipment Profile = ASIAIR")
    w("Camera = ASIAIR guide camera, gain = N/A, full size = N/A, Binning = 2, pixel size = N/A")
    w("Exposure = %s ms" % (exp if exp is not None else "N/A"))
    w("Mount = ZWO AM3/AM5/AM7, connected, guiding enabled, xAngle = N/A, xRate = N/A, yAngle = N/A, yRate = N/A, guide rate = %s" % (rate if rate is not None else "N/A"))
    w("X guide algorithm = Hysteresis, Hysteresis = %s, Aggression = %s, Minimum move = %s" % (ap_[("ra", "hysteresis")], ap_[("ra", "aggression")], ap_[("ra", "minMove")]))
    w("Y guide algorithm = Resist Switch, Minimum move = %s Aggression = %s FastSwitch = %s" % (ap_[("dec", "minMove")], ap_[("dec", "aggression")], ap_[("dec", "fastSwitch")]))
    ra, dec, lst = si.get("RA"), si.get("Dec"), si.get("sidereal_time")
    ha = ((lst - ra + 12) % 24) - 12 if ra is not None and lst is not None else None
    w("RA = %s hr, Dec = %s deg, Hour angle = %s hr, Pier side = %s, Rotator pos = N/A, Alt = %s deg, Az = %s deg" % (
        "%.2f" % ra if ra is not None else "N/A", "%.1f" % dec if dec is not None else "N/A", "%.2f" % ha if ha is not None else "N/A",
        str(si.get("pier_side", "N/A")).capitalize(), "%.1f" % si["Alt"] if "Alt" in si else "N/A", "%.1f" % si["Az"] if "Az" in si else "N/A"))
    w(COLS)
    in_section = True; steps.clear()
    log("guiding section begins (%s): scale %.2f\"/px, exposure %s ms, RA aggr %s, Dec aggr %s" % (why, scale, exp, ap_[("ra", "aggression")], ap_[("dec", "aggression")]))

def end(why):
    global in_section
    if in_section:
        w("Guiding Ends at %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")); in_section = False
        log("guiding section ends (%s)" % why)

def fmt(v, p=3):
    return "" if v is None else ("%.*f" % (p, v))

def step(m, e):
    global last_frame, n_total
    fr = e.get("Frame")
    if not in_section or (last_frame is not None and fr is not None and fr < last_frame):
        begin(m, "first step" if not in_section else "frame counter reset")
    last_frame = fr
    ra, de = e.get("RADistanceRaw"), e.get("DECDistanceRaw")
    rg, dg = e.get("RADistanceGuide"), e.get("DECDistanceGuide")
    px = lambda v: None if v is None else v / scale
    rd, dd = e.get("RADirection") or "", e.get("DECDirection") or ""
    w(",".join([str(fr), fmt(e.get("Time")), '"Mount"', fmt(e.get("dx")), fmt(e.get("dy")), fmt(px(ra)), fmt(px(de)), fmt(px(rg)), fmt(px(dg)),
                str(e.get("RADuration") or 0) if rd else "", rd[:1] if rd and rd != "null" else "",
                str(e.get("DECDuration") or 0) if dd else "", dd[:1] if dd and dd != "null" else "",
                "", "", str(e.get("StarMass", "")), fmt(e.get("SNR"), 2), "0"]))
    if ra is not None and de is not None and not e.get("IsSettle") and not e.get("IsDither"):
        steps.append((ra, de, e.get("SNR"))); del steps[:-a.rms_window]
    n_total += 1

def info(e):
    ev = e.get("Event"); d = {k: v for k, v in e.items() if k not in ("Event", "Timestamp", "Host", "Inst")}
    if ev in ("StarLost",):
        w('%s,%s,"DROP",,,,,,,,,,,,,%s,%s,%s,"%s"' % (d.get("Frame", ""), fmt(d.get("Time")), d.get("StarMass", 0), fmt(d.get("SNR"), 2), d.get("ErrorCode", ""), d.get("Status", "")))
    elif in_section:
        w("INFO: %s %s" % (ev, " ".join("%s=%s" % kv for kv in d.items())))
    log("%s %s" % (ev, d))

def rms():
    if len(steps) < 3: return "n/a"
    r = [s[0] for s in steps]; d = [s[1] for s in steps]
    sd = lambda x: math.sqrt(sum((v - sum(x) / len(x)) ** 2 for v in x) / len(x))
    sn = [s[2] for s in steps if s[2] is not None]
    return 'RMS RA %.2f" Dec %.2f" total %.2f" (mean %+.2f/%+.2f", SNR %.0f, last %d)' % (
        sd(r), sd(d), math.hypot(sd(r), sd(d)), sum(r) / len(r), sum(d) / len(d), sum(sn) / len(sn) if sn else 0, len(steps))

QUIET = {"GuideStep", "PiStatus", "LoopingExposures", "Version", "ScopeTrack", "MountStatus"}
m = None; t_poll = t_hb = 0
try:
    while True:
        if m is None or not m.alive:
            if m is not None: log("4400 connection dropped -- reconnecting")
            try:
                m = Air(a.host, 4400, timeout=10); log("connected to %s:4400" % a.host)
            except Exception as ex:
                log("connect failed (%s) -- retry in 10 s" % ex); m = None; time.sleep(10); continue
        now = time.time()
        if now - t_poll >= a.poll:
            t_poll = now; s = res(m, "get_app_state")
            if s and s != state:
                log("app state %s -> %s" % (state, s))
                if s in ("Stopped", "Idle", "Looping") and state in ("Guiding", "LostLock", "Paused"): end("app state " + s)
                state = s
        for e in m.drain_events():
            ev = e.get("Event")
            if ev == "GuideStep": step(m, e)
            elif ev in ("StartGuiding",): begin(m, "StartGuiding")
            elif ev in ("GuidingStopped",): info(e); end("GuidingStopped")
            elif ev not in QUIET: info(e)
        if now - t_hb >= a.heartbeat:
            t_hb = now; log("%s | %d steps logged | %s" % (state, n_total, rms()))
        time.sleep(0.25)
except KeyboardInterrupt:
    pass
finally:
    end("logger stopped"); f.close()
    if m: m.close()
    log("stopped; log at %s" % path)
