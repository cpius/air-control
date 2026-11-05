#!/usr/bin/env python3
"""Live ADC readout on a planet: one line every --frames rtmp frames with the residual blue-minus-red
dispersion, while you turn the ADC levers or the ADC+camera. Keeps the planet on the rtmp crop by itself.

|B-R| (the total) is the number to drive to zero -- it does not depend on the camera angle. The
toward-zenith / across split uses sky-east on the sensor, which a camera turn changes; the loop
re-measures that (a 40" north nudge) whenever a re-centre does not converge.

Planet lost from the rtmp crop (a lever move deviates the beam) -> full-field preview frame, find it,
nudge it back, return to rtmp.

    ASIAIR_HOST=192.168.1.39 python3 -u planetary/adclive.py --east=0.061,-0.998 --minutes 30
"""
import argparse, datetime as dt, math, os, signal, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import json, cv2
from daypipes import Pipes, log
from slowpulse import Nudger

ap = argparse.ArgumentParser()
ap.add_argument("--east", required=True, help="sky east on the sensor x,y (camangle / donutcentre_live)")
ap.add_argument("--scale", type=float, default=0.162, help="bin-1 arcsec/px")
ap.add_argument("--ra", type=float, default=0.6278, help="planet RA JNow, h"); ap.add_argument("--dec", type=float, default=1.147)
ap.add_argument("--lat", type=float, default=55.689); ap.add_argument("--lon", type=float, default=12.5553)
ap.add_argument("--frames", type=int, default=10, help="frames per reading")
ap.add_argument("--exp-ms", type=float, default=60); ap.add_argument("--gain", type=int, default=300)
ap.add_argument("--recentre-px", type=float, default=300, help="re-centre when the planet is this far from the crop centre")
ap.add_argument("--minutes", type=float, default=30)
ap.add_argument("--pre-north", type=float, default=0.0, help="arcsec to move the pointing north before starting (catch up a known drift)")
ap.add_argument("--pre-east", type=float, default=0.0)
ap.add_argument("--calibrate", action="store_true", help="measure north on the sensor with a 40\" nudge before the first reading")
a = ap.parse_args()
signal.signal(signal.SIGTERM, lambda s, f: (_ for _ in ()).throw(SystemExit(128 + s)))
HOST = os.environ["ASIAIR_HOST"]
E = np.array([float(v) for v in a.east.split(",")]); E /= np.linalg.norm(E)
N = np.array([-E[1], E[0]])
CROP0 = np.array([960.0, 540.0])           # rtmp crop origin on the sensor (bin 1)
CC = np.array([960.0, 540.0])              # crop centre, crop coords

def geometry():
    u = dt.datetime.now(dt.timezone.utc); d = u.timestamp() / 86400.0 + 2440587.5 - 2451545.0
    lst = ((18.697374558 + 24.06570982441908 * d) % 24 + a.lon / 15) % 24
    H = math.radians(((lst - a.ra + 12) % 24 - 12) * 15); phi, dl = math.radians(a.lat), math.radians(a.dec)
    q = math.atan2(math.sin(H), math.tan(phi) * math.cos(dl) - math.sin(dl) * math.cos(H))
    alt = math.degrees(math.asin(math.sin(phi) * math.sin(dl) + math.cos(phi) * math.cos(dl) * math.cos(H)))
    return q, alt

def cen(c):
    """planet centroid on one Bayer plane, or None. A real disc: smoothed peak >= 40 and >= 40 plane px
    above a quarter of it -- a hot pixel passed the old test (peak 15) and read as a 62\" B-R (2026-11-04)."""
    c = c - np.median(c); sm = ndimage.gaussian_filter(c, 2); mx = float(sm.max())
    if mx < 40: return None, mx
    m = sm > 0.25 * mx; lab, n = ndimage.label(m)
    y, x = np.unravel_index(np.argmax(sm), c.shape); k = lab[y, x]
    if (lab == k).sum() < 40: return None, mx
    w = np.where(lab == k, c, 0); yy, xx = np.indices(c.shape)
    return np.array([(w * xx).sum(), (w * yy).sum()]) / w.sum(), mx

def ring_axis(c):
    """Saturn's ring axis on the sensor from second moments of the bright blob: (cos 2a, sin 2a), elongation"""
    c = c - np.median(c); sm = ndimage.gaussian_filter(c, 2); m = sm > 0.15 * sm.max()
    lab, n = ndimage.label(m); y, x = np.unravel_index(np.argmax(sm), sm.shape); w = np.where(lab == lab[y, x], c, 0)
    yy, xx = np.indices(c.shape); W = w.sum(); mx, my = (w * xx).sum() / W, (w * yy).sum() / W
    u20, u02, u11 = (w * (xx - mx) ** 2).sum() / W, (w * (yy - my) ** 2).sum() / W, (w * (xx - mx) * (yy - my)).sum() / W
    l = np.linalg.eigvalsh([[u20, u11], [u11, u02]])
    a2 = math.atan2(2 * u11, u20 - u02)
    return np.array([math.cos(a2), math.sin(a2)]), math.sqrt(l[1] / max(l[0], 1e-9))

def measure(p, n):
    offs, pos, peaks, axes = [], [], [], []; last = None
    for _ in range(n * 2):
        img, w, h, info = p.grab()
        if not info.get("fresh"): continue
        f = img.astype(np.float32)
        R, B = f[0::2, 0::2], f[1::2, 1::2]          # RGGB, R at even/even (crop origin is even)
        cr, pr = cen(R); cb, pb = cen(B)
        if cr is None or cb is None: continue
        o = (cb - cr - 0.5) * 2 * a.scale
        if np.hypot(*o) > 6.0: continue                    # R and B found different things
        edge = min(cr[0], cr[1], R.shape[1] - cr[0], R.shape[0] - cr[1])
        if edge < 40: continue                             # disc clipped by the crop edge biases both centroids
        offs.append(o); pos.append(cr * 2); peaks.append(max(pr, pb)); last = (img, cr * 2, cb * 2 + 1)
        ax, el = ring_axis(R)
        if el > 1.3: axes.append(ax)
        if len(offs) >= n: break
    if not offs: return None
    ang = 0.5 * math.atan2(*np.mean(axes, axis=0)[::-1]) if axes else None     # ring axis angle on the sensor, rad
    return np.median(np.array(offs), axis=0), np.median(np.array(pos), axis=0), float(np.max(peaks)), float(np.std(np.array(offs), axis=0).mean()), last, ang

def rtmp():
    p = Pipes(); p.setup("rtmp", a.exp_ms / 1000, a.gain, 1); return p

def find_full():
    """full-field preview frame -> planet position on the sensor (bin 1) or None"""
    q = Pipes(); q.setup("preview", 0.05, a.gain, 2)
    try:
        q.grab(timeout=40); img, *_ = q.grab(timeout=40)
    finally:
        q.close()
    sm = ndimage.gaussian_filter(img.astype(np.float64), 3); sky = float(np.median(sm))
    if sm.max() - sky < 2000: return None
    y, x = np.unravel_index(int(np.argmax(sm)), sm.shape); return np.array([x, y], float) * 2

# --- dashboard feed: ~/ASICAP/dashboard/data/adc-YYYY-MM-DD.jsonl + one PNG per reading (page /adc) ---
DASH = os.environ.get("ASICAP_DASHBOARD_DIR", os.path.expanduser("~/ASICAP/dashboard/data"))   # as lib/recorder.py
IMGDIR = os.path.expanduser("~/ASICAP/telemetry/%s/adc" % time.strftime("%Y-%m-%d"))
os.makedirs(DASH, exist_ok=True); os.makedirs(IMGDIR, exist_ok=True)
def feed(row):
    row = dict(t=dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), **row)
    try:
        with open(os.path.join(DASH, "adc-%s.jsonl" % time.strftime("%Y-%m-%d")), "a") as fh: fh.write(json.dumps(row) + "\n")
    except Exception as e:
        log("dashboard feed failed: %s" % e)

def crop_png(last, tot):
    """colour crop of the planet with the blue-minus-red offset drawn 20x as an arrow (R centroid -> B)."""
    try:
        img, cr, cb = last
        bgr = cv2.cvtColor(np.ascontiguousarray(img.astype(np.uint8)), cv2.COLOR_BayerBG2BGR)
        x, y = int(round(cr[0])), int(round(cr[1])); H = 150
        x0, y0 = max(0, x - H), max(0, y - H); c = bgr[y0:y0 + 2 * H, x0:x0 + 2 * H].astype(np.float32)
        c = np.clip(c / max(float(np.percentile(c, 99.8)), 1.0) * 255, 0, 255).astype(np.uint8)
        c = cv2.resize(c, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        p0 = np.array([(cr[0] - x0) * 2, (cr[1] - y0) * 2]); p1 = p0 + (np.array(cb) - np.array(cr) - 1) * 2 * 20
        cv2.arrowedLine(c, tuple(int(v) for v in p0), tuple(int(v) for v in p1), (255, 255, 0), 2, tipLength=0.25)
        cv2.putText(c, '%.2f"' % tot, (10, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
        cv2.putText(c, time.strftime("%H:%M:%S"), (10, c.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
        path = os.path.join(IMGDIR, time.strftime("%H%M%S") + ".png"); cv2.imwrite(path, c); return path
    except Exception as e:
        log("crop image failed: %s" % e); return None

REF = dict(ang=None)             # ring-axis reference: angle on the sensor when E/N were last known
HIST = []                        # (up, across) per reading, for the right-way / wrong-way hint
def advise(up, ac, tot, th=0.15):
    """one plain instruction for the ADC levers + a hint from what the last move did"""
    # Mads wants an instruction even when it is good enough: only "spot on" (both parts within the
    # ~0.05" reading noise) stops the advice; under 0.30" it is marked as fine-tuning.
    if abs(up) < 0.06 and abs(ac) < 0.06:
        adv, key = "SPOT ON - leave it", None
    elif abs(ac) > abs(up):
        adv, key = "MOVE TOGETHER (or rotate camera)", 1
    elif up > 0:
        adv, key = "OPEN MORE", 0
    else:
        adv, key = "CLOSE MORE", 0
    hint = ""
    if key is not None:
        hint = "sideways %.2f\"" % abs(ac) if key == 1 else "%.2f\" to go" % abs(up)
        if tot < 0.30: hint += ", already good - fine-tuning"
    HIST.append((up, ac))
    if key is not None and len(HIST) >= 5:
        # tiny lever moves (Mads, 11-04): one reading scatters ~0.05", so compare the mean of the last 2
        # readings with the 3 before them (difference noise ~0.03") -> verdict ~8 s after a move
        now = np.mean([abs(h[key]) for h in HIST[-2:]]); then = np.mean([abs(h[key]) for h in HIST[-5:-2]])
        if now - then < -0.06: hint = "right way - keep going (" + hint + ")"
        elif now - then > 0.06: hint = "wrong way - go back (" + hint + ")"
    return adv, hint

nud = Nudger(HOST, log=log)
def nudge_to_centre(sensor_pos, why):
    global E, N
    d = sensor_pos - (CROP0 + CC); e_as, n_as = float(d @ E) * a.scale, float(d @ N) * a.scale
    log("re-centre (%s): planet %+.0f\" east %+.0f\" north of centre" % (why, e_as, n_as))
    print(">> %s   (moving Saturn back to the centre)" % time.strftime("%H:%M:%S"), flush=True); feed(dict(kind="status", text="moving Saturn back to the centre"))
    nud.nudge(e_as, n_as)

def calibrate(p):
    """camera may have been turned: 40" north nudge -> north on the sensor"""
    global E, N
    m0 = measure(p, 4)
    nud.nudge(0, 40.0); time.sleep(0.8)
    m1 = measure(p, 4)
    if m0 is None or m1 is None: log("calibration failed (planet not on the crop)"); return
    dlt = m1[1] - m0[1]
    if np.linalg.norm(dlt) < 40: log("calibration: image moved only %.0f px -- keeping east %s" % (np.linalg.norm(dlt), np.round(E, 3))); return
    N = -dlt / np.linalg.norm(dlt); E = np.array([N[1], -N[0]]); REF["ang"] = None
    log("calibration: 40\" north moved the image %.0f px -> east on sensor (%.3f, %.3f)" % (np.linalg.norm(dlt), E[0], E[1]))

if a.pre_north or a.pre_east:
    log("pre-nudge %s" % nud.nudge(a.pre_east, a.pre_north))
p = rtmp(); T0 = time.time(); prev = None; lost = 0; last_dist = None
if a.calibrate:
    calibrate(p)
q0, alt0 = geometry()
print(">> Colour error on Saturn. SMALLER IS BETTER. Goal: under 0.30\".  (No ADC at all would give %.2f\".)" % (0.76 / math.tan(math.radians(alt0))), flush=True)
print(">> Do what it says: OPEN MORE / CLOSE MORE = spread or close the two levers.  MOVE TOGETHER = swing both levers the same way (or rotate the camera).", flush=True)
print(">> Tiny moves: after each one wait ~10 s for 'right way - keep going' or 'wrong way - go back'.", flush=True)
try:
    while time.time() - T0 < a.minutes * 60:
        m = measure(p, a.frames)
        if m is None:
            lost += 1; log("planet not on the rtmp crop (%d)" % lost)
            print(">> %s   (Saturn moved off the screen - looking for it)" % time.strftime("%H:%M:%S"), flush=True); feed(dict(kind="status", text="Saturn moved off the screen - looking for it"))
            if lost >= 2:
                p.close(); s = find_full()
                if s is None: log("not in the full field either -- waiting 5 s"); time.sleep(5)
                else: nudge_to_centre(s, "lost from the crop")
                p = rtmp(); lost = 0
            continue
        lost = 0
        d, pos, peak, sd, last, ang = m
        # camera turned? Saturn's rings are fixed on the sky, so their angle on the sensor tracks the camera
        if ang is not None:
            if REF["ang"] is None:
                REF.update(ang=ang, E=E.copy(), N=N.copy())
            else:
                dl = (ang - REF["ang"] + math.pi / 2) % math.pi - math.pi / 2
                Rm = np.array([[math.cos(dl), -math.sin(dl)], [math.sin(dl), math.cos(dl)]])
                E, N = Rm @ REF["E"], Rm @ REF["N"]
                if abs(math.degrees(dl) - REF.get("logged", 0)) > 3:
                    log("camera turned %+.0f deg since the reference (ring axis) -> east on sensor (%.3f, %.3f)" % (math.degrees(dl), E[0], E[1]))
                    REF["logged"] = math.degrees(dl)
        q, alt = geometry(); UP = math.cos(q) * N + math.sin(q) * E; AC = np.array([-UP[1], UP[0]])
        tot, up, ac = float(np.hypot(*d)), float(d @ UP), float(d @ AC)
        exp_atm = 0.76 / math.tan(math.radians(alt))
        trend = "" if prev is None else ("  better" if tot < prev - 0.05 else ("  WORSE" if tot > prev + 0.05 else "  same"))
        bar = "#" * min(40, int(round(tot / 0.05)))
        sat = "  SATURATED" if peak >= 250 else ""
        log("|B-R| %4.2f\"  up %+5.2f  across %+5.2f  (no-ADC %.2f\", alt %.1f)%s%s  [sd %.2f, peak %.0f, planet %d,%d]" % (
            tot, up, ac, exp_atm, alt, trend, sat, sd, peak, pos[0], pos[1]))
        advice, hint = advise(up, ac, tot)
        # the plain line Mads watches (the terminal shows only lines starting with ">>")
        print(">> %s   %4.2f\"  %-24s %s   %s" % (time.strftime("%H:%M:%S"), tot, "#" * min(24, int(round(tot / 0.05))),
              advice, hint + (" (too bright)" if sat else "")), flush=True)
        feed(dict(kind="reading", tot=round(tot, 3), up=round(up, 3), across=round(ac, 3), alt=round(alt, 2), no_adc=round(exp_atm, 3),
                  trend=trend.strip().lower(), peak=round(peak), planet=[int(pos[0]), int(pos[1])], img=crop_png(last, tot) if last else None,
                  advice=advice, hint=hint))
        prev = tot
        dist = float(np.linalg.norm(pos - CC))
        if dist > a.recentre_px:
            if last_dist is not None and dist > 0.8 * last_dist and last_dist > a.recentre_px:
                calibrate(p)
            nudge_to_centre(CROP0 + pos, "%.0f px off" % dist); last_dist = dist
            time.sleep(0.8)
        else:
            last_dist = None
finally:
    try: p.close()
    except Exception: pass
    log("adclive stopped")
