#!/usr/bin/env python3
"""Hold Saturn centred and measure tip-tilt seeing every --every-min minutes, until --until.

Hold: focus page bin 1 (1:1 centre crop), one fresh frame every --hold-s; if the planet is more than
--deadband arcsec off, slowpulse.Nudger moves the pointing by --hold-gain of the error (1x, mount-timed
dead-man moves, Dec backlash compensated). Planet missing from the crop -> re-centre on the full field
(preview bin 2, planetcentre's method). Missing from the full field too -> NO blind search: log the sky
level (cloud vs mispointing) and retry every 30 s.

Seeing: a burst of --burst frames with the hold paused; robust (MAD) frame-to-frame centroid motion per
sky axis -> G-tilt r0 -> FWHM at 550 nm (tiltseeing.py's formula). Mount shake adds in: an UPPER bound,
best compared between bursts/nights. Also logs Saturn's flux per ms (cloud/dew indicator), true altitude,
and the Air's input voltage.

Every pass re-asserts tracking and Sidereal rate; a pier-side change stops the loop (camera angle and
Dec sense flip).

    ASIAIR_HOST=192.168.1.36 python3 -u planetary/satseeing.py --east=-0.012,-1.000 --until 02:30
"""
import argparse, csv, datetime as dt, math, os, signal, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from daypipes import host, Pipes, save_png
from air_rpc import Air
from slowpulse import Nudger
from moonephem import jd_from_utc, gmst_deg, altaz

ap = argparse.ArgumentParser()
ap.add_argument("--east", default="-0.012,-1.000", help="sky east on the sensor (x right, y down)")
ap.add_argument("--arcsec-per-px", type=float, default=0.1866, help="bin 1")
ap.add_argument("--ra", type=float, default=11.7195 / 15, help="Saturn JNow RA, hours (for the true altitude)")
ap.add_argument("--dec", type=float, default=2.087)
ap.add_argument("--every-min", type=float, default=5.0)
ap.add_argument("--burst", type=int, default=120)
ap.add_argument("--hold-s", type=float, default=4.0)
ap.add_argument("--deadband", type=float, default=10.0, help="arcsec")
ap.add_argument("--hold-gain", type=float, default=0.7)
ap.add_argument("--exp-ms", type=float, default=15.0)
ap.add_argument("--gain", type=int, default=300)
ap.add_argument("--peak-lo", type=float, default=20000); ap.add_argument("--peak-hi", type=float, default=45000)
ap.add_argument("--until", default="02:30", help="local HH:MM (after midnight is fine)")
ap.add_argument("--video-below", type=float, default=0.0, help="FWHM arcsec: a burst below this records a clip (0 = never)")
ap.add_argument("--video-seconds", type=float, default=240.0)
ap.add_argument("--video-args", default="--roi 1000 --exp-ms 40 --gain 350 --exp-max 100 --gain-max 450 --deadband 100 --min-gap 4",
                help="satvideo flags besides --seconds/--hold-mode/--east/--arcsec-per-px")
ap.add_argument("--outdir", default=os.path.expanduser("~/ASICAP/telemetry/%s/satseeing" % time.strftime("%Y-%m-%d")))
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)
E = np.array([float(v) for v in a.east.split(",")]); E /= np.linalg.norm(E); N = np.array([-E[1], E[0]])
LAM, D, R2A = 550e-9, 0.203, 206265.0
SC = a.arcsec_per_px

def log(s): print("%s  %s" % (time.strftime("%H:%M:%S"), s), flush=True)
def _term(signum, _f): raise SystemExit(128 + signum)          # finish any move, run the finally blocks
signal.signal(signal.SIGTERM, _term)

def until_ts():
    h, m = [int(v) for v in a.until.split(":")]
    now = dt.datetime.now(); t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if t < now - dt.timedelta(hours=12): t += dt.timedelta(days=1)
    elif t < now: t += dt.timedelta(days=1) if (now - t) > dt.timedelta(hours=12) else dt.timedelta(0)
    return t.timestamp()
END = until_ts()

def true_altaz():
    t = dt.datetime.now(dt.timezone.utc)
    return altaz(a.ra * 15, a.dec, (gmst_deg(jd_from_utc(t)) + 12.555278) % 360, 55.689444)

def mdo(fn):
    for k in range(3):
        m = None
        try:
            m = Air(host(), 4400, timeout=10); return fn(m)
        except Exception as e:
            log("  4400 call failed (%s) -- retry %d" % (e, k + 1)); time.sleep(1.5)
        finally:
            if m is not None:
                try: m.close()
                except Exception: pass
    raise RuntimeError("4400 unreachable")

def r(m, meth, p=None): return m.call(meth, p or [])["result"]

PIER = None
def check_mount():
    """Re-assert tracking + Sidereal; stop on a pier change. Returns the state dict."""
    global PIER
    def f(m):
        s = r(m, "scope_get_info")
        if not s.get("is_enable_track"):
            log("  TRACKING WAS OFF -- clearing moves and re-enabling")
            r(m, "scope_move", ["none"])
            try: r(m, "scope_abort_slew")
            except Exception: pass
            r(m, "scope_set_track_state", [True])
        tm = r(m, "scope_get_track_mode")
        if tm.get("index") != 0:
            log("  track mode was %s -- setting Sidereal" % tm); r(m, "scope_set_track_mode", ["Sidereal"])
        return s
    s = mdo(f)
    if PIER is None: PIER = s["pier_side"]
    elif s["pier_side"] != PIER:
        raise SystemExit("pier side changed %s -> %s: camera angle and Dec sense flipped -- stopping" % (PIER, s["pier_side"]))
    return s

def volts():
    try: return mdo(lambda m: r(m, "scope_get_input_voltage"))
    except Exception as e: return "n/a (%s)" % e

def find(img, floor=2000.0):
    """Planet centroid (bin-1 px of this frame), or None. Smoothed so hot pixels cannot win."""
    x = img.astype(np.float32); bg = float(np.median(x[::5, ::5]))
    sm = ndimage.gaussian_filter(x, 2); pk = float(sm.max()) - bg
    if pk < floor: return None, bg, pk, 0.0
    lab, n = ndimage.label(sm > bg + 0.3 * pk)
    sizes = ndimage.sum(np.ones_like(sm), lab, range(1, n + 1)); k = int(np.argmax(sizes)) + 1
    if sizes[k - 1] < 150: return None, bg, pk, 0.0
    w = np.clip(x - bg, 0, None) * (lab == k)
    cy, cx = ndimage.center_of_mass(w)
    flux = float(np.clip(x - bg, 0, None)[ndimage.binary_dilation(lab == k, iterations=15)].sum())
    return (cx, cy), bg, float(x.max()) - bg, flux

nudger = Nudger(host(), dec_up="auto", log=log)
def move(e_as, n_as):
    done = nudger.nudge(e_as, n_as); time.sleep(0.6)
    return ", ".join("%s %.0f\"" % (c, amt) for c, amt, _, _ in done) or "nothing"

p = Pipes()
state = dict(exp=a.exp_ms / 1000.0, gain=a.gain, page=None)
def use(page, exp, gain, binning):
    if state["page"] != (page, binning):
        p.setup(page, exp, gain, binning); p.grab(timeout=40); state["page"] = (page, binning)
        state["exp"], state["gain"] = exp, gain
def set_exp(exp, gain):
    if abs(exp - state["exp"]) > 1e-6:
        p.c("set_control_value", ["Exposure", int(round(exp * 1e6))]); p.exp = exp; state["exp"] = exp
    if gain != state["gain"]:
        p.c("set_control_value", ["Gain", int(gain)]); state["gain"] = gain
    p.s.air.drain_events(); time.sleep(0.2); p.grab(timeout=40)

def focus_mode():
    if state["page"] != ("focus", 1):
        p.setup("focus", state["exp"], state["gain"], 1); p.grab(timeout=40); state["page"] = ("focus", 1)

def autoexp(img_peak):
    """Keep the planet peak inside [peak-lo, peak-hi]: exposure 3-25 ms first, then gain."""
    if a.peak_lo <= img_peak <= a.peak_hi: return False
    want = 0.5 * (a.peak_lo + a.peak_hi); f = want / max(img_peak, 500.0)
    if img_peak >= 64000: f = min(f, 0.4)
    exp, gain = state["exp"] * f, state["gain"]
    if exp > 0.025: gain = min(450, gain + int(round(200 * math.log10(exp / 0.025)))); exp = 0.025
    if exp < 0.003: gain = max(100, gain + int(round(200 * math.log10(exp / 0.003)))); exp = 0.003
    log("  exposure %.1f ms g%d -> %.1f ms g%d (peak %.0f)" % (state["exp"] * 1e3, state["gain"], exp * 1e3, gain, img_peak))
    set_exp(exp, gain); return True

def fullfield_centre():
    """planetcentre's method on the whole sensor. Returns True when the planet is within 20"."""
    p.setup("preview", 0.05, 200, 2); p.grab(timeout=40); state["page"] = ("preview", 2)
    ok = False
    for it in range(3):
        img, w, h, _ = p.grab(timeout=40); img = img.astype(np.float64)
        med = np.median(img); sm = ndimage.gaussian_filter(img, 3); pk = sm.max() - med
        if pk < 1500:
            log("  full field: NO planet (peak %.0f over sky %.0f) -- cloud if the sky is ~2000+ over bias, else mispointed" % (pk, med)); break
        lab, n = ndimage.label(sm > med + 0.25 * pk); k = int(np.argmax(ndimage.sum(np.ones_like(sm), lab, range(1, n + 1)))) + 1
        cy, cx = ndimage.center_of_mass(np.clip(sm - med, 0, None), lab, k)
        d = np.array([2 * cx - 1920, 2 * cy - 1080]); e_as, n_as = float(d @ E) * SC, float(d @ N) * SC
        log("  full field pass %d: %+.0f\" east %+.0f\" north" % (it, e_as, n_as))
        if abs(e_as) < 20 and abs(n_as) < 20: ok = True; break
        log("    nudged " + move(e_as, n_as))
    state["page"] = None
    p.setup("focus", state["exp"], state["gain"], 1); p.grab(timeout=40); state["page"] = ("focus", 1)
    return ok

hold_csv = open(os.path.join(a.outdir, "hold_%s.csv" % time.strftime("%H%M%S")), "a", buffering=1)
hw = csv.writer(hold_csv); hw.writerow(["t", "x", "y", "east_as", "north_as", "peak", "moved"])
see_csv = open(os.path.join(a.outdir, "seeing_%s.csv" % time.strftime("%H%M%S")), "a", buffering=1)
sw = csv.writer(see_csv); sw.writerow(["time", "alt", "az", "n", "dt_med", "sig_ra", "sig_dec", "r0_cm", "fwhm", "fwhm_dec", "fwhm_zenith", "exp_ms", "gain", "peak", "flux_per_ms_norm", "volts"])

def burst():
    focus_mode()
    img, *_ = p.grab(timeout=40); c, bg, pk, _ = find(img)
    if c is None: log("SEEING skipped: planet not in the crop"); return
    for _ in range(3):
        if not autoexp(pk): break
        img, *_ = p.grab(timeout=40); c, bg, pk, _ = find(img)
        if c is None: log("SEEING skipped: planet lost while setting exposure"); return
    stamp = time.strftime("%H%M%S")
    t, xs, ys, pks, fl = [], [], [], [], []
    for i in range(a.burst):
        img, w, h, info = p.grab(timeout=40); tt = time.time()
        c, bg, pk, flux = find(img)
        if c is None: continue
        t.append(tt); xs.append(c[0]); ys.append(c[1]); pks.append(pk); fl.append(flux)
        if i == 0:
            np.save(os.path.join(a.outdir, "%s_burst0.npy" % stamp), img.astype(np.uint16))
            save_png(img, os.path.join(a.outdir, "%s_burst0.png" % stamp))
    with open(os.path.join(a.outdir, "%s_burst.csv" % stamp), "w") as f:
        cw = csv.writer(f); cw.writerow(["t", "x", "y", "peak", "flux"]); cw.writerows(zip(t, xs, ys, pks, fl))
    if len(t) < 30: log("SEEING skipped: only %d frames with the planet" % len(t)); return
    t, xs, ys = map(np.array, (t, xs, ys))
    dtt = np.diff(t); ok = (dtt > 0) & (dtt < 2.0)
    # sky axes: east = E . d, north = N . d
    de = (np.diff(xs) * E[0] + np.diff(ys) * E[1])[ok] * SC; dn = (np.diff(xs) * N[0] + np.diff(ys) * N[1])[ok] * SC
    rob = lambda d: 1.4826 * float(np.median(np.abs(d - np.median(d)))) / math.sqrt(2)
    sra, sdec = rob(de), rob(dn)
    def fwhm_of(sig_as):
        s = sig_as / R2A; r0 = D * (0.17 * (LAM / D) ** 2 / s ** 2) ** 0.6
        return r0, 0.98 * LAM / r0 * R2A
    r0, fw = fwhm_of(math.sqrt((sra ** 2 + sdec ** 2) / 2)); _, fwd = fwhm_of(sdec)
    alt, az = true_altaz(); fz = fw * math.cos(math.radians(90 - alt)) ** 0.6
    norm = float(np.median(fl)) / (state["exp"] * 1e3) / 10 ** (state["gain"] / 200.0)
    if "flux0" not in state: state["flux0"] = norm
    v = volts()
    log("SEEING %s alt %.1f az %.0f: FWHM %.2f\" (Dec axis %.2f\", zenith-equiv %.2f\")  r0 %.1f cm  tilt RA %.2f\" Dec %.2f\"  "
        "n %d dt %.2fs  %.1f ms g%d peak %.0f  flux %.2f of first  %s V"
        % (time.strftime("%H:%M"), alt, az, fw, fwd, fz, r0 * 100, sra, sdec, len(t), float(np.median(dtt)),
           state["exp"] * 1e3, state["gain"], float(np.median(pks)), norm / state["flux0"], v))
    sw.writerow([time.strftime("%H:%M:%S"), "%.2f" % alt, "%.1f" % az, len(t), "%.3f" % float(np.median(dtt)), "%.3f" % sra, "%.3f" % sdec,
                 "%.1f" % (r0 * 100), "%.2f" % fw, "%.2f" % fwd, "%.2f" % fz, "%.1f" % (state["exp"] * 1e3), state["gain"],
                 "%.0f" % float(np.median(pks)), "%.3f" % (norm / state["flux0"]), v])
    return fw

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
def run_logged(cmd, logpath, keep, timeout):
    """Run a child tool from the repo root, streaming the lines that matter; return its exit code."""
    import subprocess
    log("  $ " + " ".join(cmd))
    with open(logpath, "w") as lf:
        ch = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        t0 = time.time()
        try:
            for line in ch.stdout:
                lf.write(line); lf.flush()
                if any(k in line for k in keep): log("   | " + line.rstrip()[:200])
                if time.time() - t0 > timeout:
                    log("  child over %.0f s -- SIGTERM" % timeout); ch.terminate()
            return ch.wait()
        except BaseException:
            ch.terminate(); ch.wait(); raise

def run_video(fw):
    """Hand the rig to satvideo for one clip, then verify the clip FILE on the Air (clipcheck)."""
    global p
    stamp = time.strftime("%Y-%m-%d-%H%M%S"); hms = stamp[-6:]
    log("VIDEO seeing %.2f\" < %.2f\" -- recording %.0f s (log %s_satvideo.log)" % (fw, a.video_below, a.video_seconds, hms))
    try: p.close()
    except Exception: pass
    state["page"] = None
    cmd = ["python3", "-u", os.path.join(HERE, "satvideo.py"), "--seconds", "%g" % a.video_seconds] + a.video_args.split() + \
          ["--hold-mode", "pulse", "--east=%s" % a.east, "--arcsec-per-px", "%g" % SC]
    rc = run_logged(cmd, os.path.join(a.outdir, "%s_satvideo.log" % hms),
                    ("exposure test", "ABORT", "RECORDED", "STOPPED", "hold:", "exposure used", "Traceback", "Error", "confirm"), a.video_seconds + 240)
    log("VIDEO satvideo exit %d (%s)" % (rc, {0: "recorded, planet held", 1: "error", 2: "bad arguments", 3: "no planet before recording",
        4: "too dim (cloud)", 5: "stopped: planet lost", 6: "stopped: frames stalled", 8: "recorder refused to write"}.get(rc, "?")))
    if rc in (0, 5, 6):
        crc = run_logged(["python3", "-u", os.path.join(HERE, "clipcheck.py"), "--air", "--host", host(), "--since", stamp, "--roi", a.video_args.split()[a.video_args.split().index("--roi") + 1] if "--roi" in a.video_args else "640"],
                         os.path.join(a.outdir, "%s_clipcheck.log" % hms), ("VERDICT", "clip ", "Traceback", "Error"), 600)
        log("VIDEO clipcheck exit %d (%s)" % (crc, {0: "planet in the clip", 2: "EMPTY clip", 3: "partial"}.get(crc, "could not verify")))
        if crc == 2:
            raise SystemExit("clip is EMPTY in the file -- not recording blind again")
    elif rc == 8:
        log("VIDEO the Air's recorder refused to write -- disabling video for the rest of the run"); a.video_below = 0.0
    p = Pipes()
    return rc

code = 1
try:
    s = check_mount()
    alt, az = true_altaz()
    log("start: pier %s, Saturn alt %.1f az %.0f, until %s, seeing every %.0f min (%d frames), hold every %.0f s, deadband %.0f\""
        % (s["pier_side"], alt, az, time.strftime("%H:%M", time.localtime(END)), a.every_min, a.burst, a.hold_s, a.deadband))
    if not fullfield_centre():
        log("could not centre on the full field at start -- will keep retrying")
    focus_mode()
    next_burst = time.time()
    lost_since = None; last_beat = 0
    while time.time() < END:
        t0 = time.time()
        s = check_mount()
        img, w, h, info = p.grab(timeout=40)
        c, bg, pk, _ = find(img)
        if c is None:
            lost_since = lost_since or time.time()
            log("HOLD planet not in the focus crop (peak %.0f over %.0f) -- full-field re-centre" % (pk, bg))
            hw.writerow(["%.2f" % time.time(), "", "", "", "", "%.0f" % pk, "lost"])
            if not fullfield_centre():
                if time.time() - lost_since > 900:
                    log("LOST for 15 min -- stopping (no blind search)"); code = 3; break
                time.sleep(30); continue
            lost_since = None; continue
        lost_since = None
        d = np.array([c[0] - w / 2.0, c[1] - h / 2.0]); e_as, n_as = float(d @ E) * SC, float(d @ N) * SC
        moved = ""
        if abs(e_as) > a.deadband or abs(n_as) > a.deadband:
            moved = move(a.hold_gain * e_as, a.hold_gain * n_as)
        hw.writerow(["%.2f" % time.time(), "%.1f" % c[0], "%.1f" % c[1], "%.1f" % e_as, "%.1f" % n_as, "%.0f" % pk, moved])
        if moved or time.time() - last_beat > 30:
            log("HOLD %+5.0f\" E %+5.0f\" N  peak %5.0f  %s" % (e_as, n_as, pk, ("-> " + moved) if moved else "(in deadband)"))
            last_beat = time.time()
        if pk >= 64000 or pk < a.peak_lo * 0.5:
            autoexp(pk)
        if time.time() >= next_burst:
            if moved:                                         # let a correction land first
                time.sleep(1.0)
            fw = burst(); next_burst += a.every_min * 60
            while next_burst < time.time(): next_burst += a.every_min * 60
            if fw is not None and a.video_below > 0 and fw < a.video_below and time.time() + a.video_seconds < END:
                run_video(fw)
                if not fullfield_centre():
                    log("after the clip: planet not on the full field -- the hold loop will keep looking")
                focus_mode()
                next_burst = time.time()                      # re-measure straight away: another clip if still good
            continue
        time.sleep(max(0.0, a.hold_s - (time.time() - t0)))
    else:
        code = 0; log("reached --until %s" % a.until)
finally:
    try: mdo(lambda m: (r(m, "scope_move", ["none"]), r(m, "scope_move", ["none"])))
    except Exception as e: log("final stop failed: %s" % e)
    try: p.close()
    except Exception: pass
    log("exit %d" % code)
