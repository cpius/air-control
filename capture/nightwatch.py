#!/usr/bin/env python3
"""Unattended deep-sky supervisor (written 2026-11-04 for an overnight M1 run while Mads slept).

Every --tick seconds, as the sole owner of the Air's control ports (dewctl may run alongside):
  * measures each new autorun sub (copied off the share, tools/framecheck.py)          -> SUB lines
  * guiding not 'Guiding' twice in a row -> loop + guide again (no recalibration)         -> GUIDE lines
  * no new sub for --stall-min while the target is up -> restart the autorun sequence     -> RUN lines
  * subs soft by --soft-delta over the post-focus baseline, or the EAF cooled --refocus-dc,
    or --refocus-min since the last focus -> pause the run, fieldsweep, park best, resume   -> FOCUS lines
  * target past the meridian by --flip-ha h -> stop, goto (pier flips), solve+sync+goto x2,
    recalibrate guiding, resume                                                             -> FLIP lines
  * Air unreachable for --dead-min -> 'AIR DOWN' and exit (the rig ran out of power)        -> DOWN line
Heartbeat every 5 min. Never gotos except the planned flip. Every action is logged with its reason.

    python3 -u capture/nightwatch.py --target M1 --ra 5.575539 --dec 22.0145 --share <mnt> --night 20261104
"""
import argparse, datetime as dt, glob, json, math, os, re, shutil, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); AC = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(AC, "lib"))
from air_rpc import Air
from findstar import jnow

ap = argparse.ArgumentParser()
ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST", "192.168.1.39"))
ap.add_argument("--target", required=True); ap.add_argument("--ra", type=float, required=True, help="J2000 h")
ap.add_argument("--dec", type=float, required=True, help="J2000 deg")
ap.add_argument("--share", required=True, help="mounted 'EMMC Images' share"); ap.add_argument("--night", required=True, help="YYYYMMDD in sub names")
ap.add_argument("--dest", default=None); ap.add_argument("--scale", type=float, default=0.414, help="bin-1 arcsec/px")
ap.add_argument("--exp", type=int, default=60); ap.add_argument("--gain", type=int, default=100); ap.add_argument("--repeat", type=int, default=150)
ap.add_argument("--tick", type=float, default=30); ap.add_argument("--stall-min", type=float, default=6)
ap.add_argument("--soft-delta", type=float, default=0.6); ap.add_argument("--refocus-dc", type=float, default=2.0)
ap.add_argument("--refocus-min", type=float, default=90); ap.add_argument("--flip-ha", type=float, default=0.3)
ap.add_argument("--no-flip", action="store_true"); ap.add_argument("--dead-min", type=float, default=10)
ap.add_argument("--min-alt", type=float, default=20)
ap.add_argument("--last-focus-ago", type=float, default=0, help="minutes since the last focus run")
a = ap.parse_args()
LAT, LON = 55.689, 12.5553
DEST = os.path.expanduser(a.dest or "~/ASICAP/%s-%s-%s Session/Light/%s" % (a.night[:4], a.night[4:6], a.night[6:], a.target)); os.makedirs(DEST, exist_ok=True)
SRC = os.path.join(a.share, "Autorun", "Light", a.target)
LOGDIR = os.path.expanduser("~/ASICAP/telemetry/%s-%s-%s" % (a.night[:4], a.night[4:6], a.night[6:]))

def say(kind, msg): print("%s %-5s %s" % (time.strftime("%H:%M:%S"), kind, msg), flush=True)

def call(port, method, params=None, timeout=20):
    """one call on a fresh socket (both ports drop idle sockets); None on any failure"""
    try:
        c = Air(a.host, port, key=os.path.join(AC, "embedded_key.pem") if port == 4700 else None, timeout=10)
        try:
            r = c.call(method, params if params is not None else [], timeout=timeout)
        finally:
            c.close()
        return r
    except Exception as e:
        return {"error": "EXC %r" % e}

def ok(r): return isinstance(r, dict) and not r.get("error")
def run(cmd, timeout):
    env = dict(os.environ, ASIAIR_HOST=a.host, EAF_MIN="0", EAF_MAX="100000")
    try:
        p = subprocess.run(cmd, cwd=AC, env=env, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or "") if isinstance(e.stdout, str) else ""

def lst_now():
    d = time.time() / 86400.0 + 2440587.5 - 2451545.0
    return ((18.697374558 + 24.06570982441908 * d) % 24 + LON / 15) % 24
def target_jnow():
    y = 2000 + (time.time() - 946728000) / (365.25 * 86400); return jnow(a.ra, a.dec, y)
def ha_alt():
    ra, de = target_jnow(); h = ((lst_now() - ra + 12) % 24) - 12
    H, p, d = math.radians(h * 15), math.radians(LAT), math.radians(de)
    return h, math.degrees(math.asin(math.sin(p) * math.sin(d) + math.cos(p) * math.cos(d) * math.cos(H)))

# ---------- subs ----------
seen = set(os.path.basename(p) for p in glob.glob(os.path.join(DEST, "*.fit")))
sizes, subs = {}, []           # subs: (time, n, fwhm, ecc)
last_sub_t = time.time()
def poll_subs():
    global last_sub_t
    try:
        nxt = (dt.date(int(a.night[:4]), int(a.night[4:6]), int(a.night[6:])) + dt.timedelta(days=1)).strftime("%Y%m%d")
        files = sorted(glob.glob(os.path.join(SRC, "*_%s-*.fit" % a.night)) + glob.glob(os.path.join(SRC, "*_%s-*.fit" % nxt)))   # names are UTC: the date rolls at 01:00 CET
    except OSError:
        return
    for p in files:
        b = os.path.basename(p)
        if b in seen: continue
        try: s = os.path.getsize(p)
        except OSError: continue
        if sizes.get(b) != s or s < 16_000_000:
            sizes[b] = s; continue
        try:
            shutil.copyfile(p, os.path.join(DEST, b))
        except OSError as e:
            say("SUB", "copy failed %s: %s" % (b, e)); continue
        seen.add(b); last_sub_t = time.time()
        rc, out = run(["python3", os.path.expanduser("~/ASICAP/tools/framecheck.py"), "--scale", str(a.scale), os.path.join(DEST, b)], 120)
        m = re.search(r"_(\d{4})\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)", out)
        if m:
            n, stars, fwhm, ecc = m.group(1), int(m.group(2)), float(m.group(3)), float(m.group(6))
            subs.append((time.time(), n, fwhm, ecc, stars))
            flag = " <-- SOFT" if base and fwhm > base + a.soft_delta else (" <-- TRAILED" if ecc > 1.6 else (" <-- FEW STARS" if stars < 60 else ""))
            say("SUB", "%s  FWHM %.2f\"  ecc %.2f  %d stars%s" % (n, fwhm, ecc, stars, flag))
        else:
            say("SUB", "%s  not measurable (cloud/dew?)" % b)

# ---------- actions ----------
def guide_state():
    r = call(4400, "get_app_state"); return r.get("result") if ok(r) else None

def restart_guiding(recal=False):
    say("GUIDE", "restarting guiding%s" % (" with recalibration" if recal else ""))
    run(["python3", "-u", "capture/guide.py", "--host", a.host, "loop"], 60)
    time.sleep(6)
    cmd = ["python3", "-u", "capture/guide.py", "--host", a.host, "start", "--wait", "600"] + (["--recalibrate"] if recal else [])
    rc, out = run(cmd, 700)
    st = guide_state(); say("GUIDE", "after restart: %s" % st); return st == "Guiding"

def start_run(why):
    say("RUN", "starting autorun (%s): %d x %d s gain %d" % (why, a.repeat, a.exp, a.gain))
    for m, p in (("stop_exposure", []), ("set_control_value", ["Gain", a.gain]), ("set_page", ["autosave"]),
                 ("set_sequence", [{"id": 1, "type": "light", "exp": a.exp, "gain": a.gain, "bin": 1, "repeat": a.repeat, "filter": 0,
                                    "suffix": "", "enable": True, "autoexp": False, "capture_index": 1}]),
                 ("set_sequence_setting", [{"group_name": a.target, "mount_end_action": "none", "delay_first": 0, "delay_between_frame": 0,
                                            "delay_between_sequence": 0, "group_by_slot": True, "focuser_go_home": False,
                                            "caa_go_home": False, "shutdown_pi": False}]),
                 ("start_exposure", ["light"])):
        r = call(4700, m, p)
        if not ok(r):
            if "cannot edit sequence" in str(r.get("error", "")) or (m == "set_sequence_setting" and "progress" in str(r)):
                say("RUN", "%s refused (paused sequence) -- resuming it unchanged" % m); continue   # 2026-11-05 01:00
            say("RUN", "%s failed: %s" % (m, r)); return False
        time.sleep(0.5)
    global last_sub_t; last_sub_t = time.time(); return True

def stop_run(why):
    say("RUN", "pausing autorun (%s)" % why); call(4700, "stop_exposure"); time.sleep(3)

def eaf():
    r = call(4700, "get_focuser_info"); return (r["result"].get("position"), r["result"].get("temperature")) if ok(r) else (None, None)

base, focus_t, focus_temp = None, time.time() - a.last_focus_ago * 60, None
def refocus(why):
    global base, focus_t, focus_temp
    pos, temp = eaf()
    if pos is None: say("FOCUS", "cannot read the EAF -- skipped"); return
    say("FOCUS", "refocus (%s) around %d, EAF %.1f C" % (why, pos, temp or float("nan")))
    stop_run("refocus")
    ps = ",".join(str(pos + d) for d in (-400, -300, -200, -100, 0, 100, 200, 300, 400))
    rc, out = run(["python3", "-u", "focus/fieldsweep.py", "--pos", ps, "--rounds", "2", "--exp", "2", "--gain", "100", "--scale", "%.3f" % (a.scale * 2),
                   "--hotmap", os.path.join(LOGDIR, "hotmap_585_bin2_warm.npy"), "--park", "best", "--tag", "%s_refocus_%s" % (a.target, time.strftime("%H%M"))], 900)
    m = re.search(r"best measured (\d+) ; parabola vertex (\d+)", out); pk = re.search(r"parked EAF at (\d+)", out)
    say("FOCUS", ("best %s, vertex %s, parked %s" % (m.group(1), m.group(2), pk.group(1) if pk else "?")) if m else "sweep failed (rc %s) -- focus left at %s" % (rc, eaf()[0]))
    focus_t, focus_temp, base = time.time(), eaf()[1], None
    start_run("after refocus")

def frame_and_solve(tag):
    """one 3 s preview frame -> ASTAP; returns (ra_h J2000, dec) or None"""
    look = "/private/tmp/claude-501/-Users-madsdorup-ASICAP/eea51e87-ed17-4db1-b264-6c63aa3f1155/scratchpad/tools/look.py"
    rc, out = run(["python3", "-u", look, "--exp", "3", "--gain", "200", "--bin", "2", "--frames", "1", "--tag", tag], 120)
    m = re.search(r"-> (\S+\.png)", out)
    npys = sorted(glob.glob(os.path.join(LOGDIR, tag + "_eaf*_0.npy")), key=os.path.getmtime)
    if not npys: say("FLIP", "no frame for %s" % tag); return None
    ra, de = target_jnow()
    rc, out = run(["python3", "-u", "pointing/astapsolve.py", "--npy", npys[-1], "--ra", "%.4f" % a.ra, "--dec", "%.3f" % a.dec, "--radius", "15",
                   "--fov", "%.3f" % (1080 * a.scale * 2 / 3600)], 200)
    m = re.search(r"SOLVED centre ICRS RA [\d.]+ deg \(([\d.]+)h\) Dec ([+-]?[\d.]+)", out)
    return (float(m.group(1)), float(m.group(2))) if m else None

def sync_goto(solved):
    y = 2000 + (time.time() - 946728000) / (365.25 * 86400); rn, dn = jnow(solved[0], solved[1], y)
    r = call(4400, "scope_sync", [rn, dn]); say("FLIP", "scope_sync -> %s" % (r.get("result") if ok(r) else r)); time.sleep(1)
    from mount import Mount
    ra, de = target_jnow(); m = Mount(a.host); m.goto(ra, de); s = m.state(); m.close()
    say("FLIP", "goto done: register RA %.4f Dec %.3f pier %s" % (s["RA"], s["Dec"], s.get("pier_side")))

flipped = a.no_flip
def flip():
    global flipped, base
    flipped = True
    say("FLIP", "meridian flip: stopping run and guiding")
    stop_run("meridian flip"); call(4400, "stop_capture"); time.sleep(3)
    from mount import Mount
    ra, de = target_jnow()
    try:
        m = Mount(a.host); before = m.state().get("pier_side"); m.goto(ra, de); s = m.state(); m.close()
    except Exception as e:
        say("FLIP", "goto failed: %r -- holding" % e); return
    say("FLIP", "pier %s -> %s, register RA %.4f Dec %.3f" % (before, s.get("pier_side"), s["RA"], s["Dec"]))
    for k in range(3):
        sv = frame_and_solve("flip%d" % k)
        if not sv: say("FLIP", "solve %d failed" % k); continue
        err = math.hypot((sv[0] - a.ra) * 15 * math.cos(math.radians(a.dec)), sv[1] - a.dec) * 60
        say("FLIP", "solve %d: target %.1f' from centre" % (k, err))
        if err < 2.0: break
        sync_goto(sv)
    if not restart_guiding(recal=True):
        say("FLIP", "guiding did not restart -- running unguided is pointless at 43\"/min; retrying once")
        restart_guiding(recal=True)
    base = None; start_run("after meridian flip")

# ---------- main loop ----------
say("START", "target %s, dest %s, flip %s at HA +%.2f h" % (a.target, DEST, "off" if a.no_flip else "on", a.flip_ha))
down_since, bad_guide, last_hb = None, 0, 0
def tick():
    global down_since, bad_guide, last_hb, base, focus_temp
    t = time.time()
    st = guide_state()
    if st is None:
        down_since = down_since or t
        if t - down_since > a.dead_min * 60:
            say("DOWN", "the Air has not answered for %.0f min -- AIR DOWN (power?). Stopping." % ((t - down_since) / 60)); sys.exit(0)
        return
    if down_since: say("UP", "the Air answers again after %.0f s" % (t - down_since)); down_since = None
    poll_subs()
    h, alt = ha_alt()
    good = [s for s in subs if s[3] < 1.6 and s[4] >= 60 and s[0] > focus_t]     # since the last focus only
    if base is None and len(good) >= 6:
        base = sorted(x[2] for x in good[:6])[3]; say("FOCUS", "baseline FWHM %.2f\" from the first 6 good subs after focus" % base)
    # guiding
    if not (st.startswith("Guid") or "Dither" in st or "Settl" in st):    # dither settles are not failures
        bad_guide += 1
        say("GUIDE", "state %s (%d in a row)" % (st, bad_guide))
        if bad_guide >= 3 and alt > a.min_alt: restart_guiding(); bad_guide = 0
    else:
        bad_guide = 0
    # meridian
    if not flipped and h > a.flip_ha and alt > a.min_alt:
        flip(); return
    # stalled run
    if t - last_sub_t > a.stall_min * 60 and alt > a.min_alt:
        start_run("no new sub for %.0f min" % ((t - last_sub_t) / 60))
    # focus
    pos, temp = eaf()
    if focus_temp is None and temp is not None: focus_temp = temp
    recent = [s[2] for s in good[-6:]]
    if base and len(recent) >= 6 and sorted(recent)[3] > base + a.soft_delta:
        refocus("last 6 subs median %.2f\" vs baseline %.2f\"" % (sorted(recent)[3], base))
    elif temp is not None and focus_temp is not None and focus_temp - temp >= a.refocus_dc:
        refocus("EAF cooled %.1f C since the last focus" % (focus_temp - temp))
    elif t - focus_t > a.refocus_min * 60:
        refocus("%.0f min since the last focus" % ((t - focus_t) / 60))
    # heartbeat
    if t - last_hb > 300:
        pw = call(4700, "pi_get_info"); uv = pw.get("result", {}).get("is_undervolt") if ok(pw) else "?"
        say("BEAT", "guide %s | subs %d (last %.0f s ago) | HA %+.2f h alt %.1f | EAF %s %.1f C | base %s | undervolt %s" % (
            st, len(subs), t - last_sub_t, h, alt, pos, temp if temp is not None else float("nan"), ("%.2f" % base) if base else "-", uv))
        last_hb = t

while True:
    try:
        tick()
    except SystemExit:
        raise
    except Exception as e:
        say("ERROR", "tick failed: %r" % e)
    time.sleep(a.tick)
