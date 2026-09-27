#!/usr/bin/env python3
"""Deep full-sensor frames for Saturn's moons, planet held at a chosen spot, then a quick stack.

Preview page, 16-bit, --bin 2, --exp s at --gain: Saturn saturates on purpose. Every frame: the
saturated blob's centroid; if it is more than --tol arcsec from the target spot (--target-east /
--target-north, arcsec from the frame centre, sky directions) the pointing is corrected with pulses
(Dec: 20x joystick, RA east: tracking pause, RA west: pulse) -- no goto. Frames saved as .npy.
Then: shift-and-mean on Saturn's centroid, asinh PNG, Horizons moon positions drawn (camera angle
--east, scale --arcsec-per-px x bin) with the detected peak near each one.

    ASIAIR_HOST=192.168.1.36 python3 -u planetary/satmoons.py --frames 60 --exp 1.0 --gain 300 --east=0.997,0.079 --target-east 50
"""
import argparse, datetime as dt, json, math, os, re, signal, sys, time, urllib.parse, urllib.request
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

ap = argparse.ArgumentParser()
ap.add_argument("--frames", type=int, default=60)
ap.add_argument("--exp", type=float, default=1.0); ap.add_argument("--gain", type=int, default=300); ap.add_argument("--bin", type=int, default=2)
ap.add_argument("--east", required=True, help="sky east on the sensor 'x,y' (camangle)")
ap.add_argument("--target-east", type=float, default=0.0, help="where Saturn should sit, arcsec EAST of the frame centre")
ap.add_argument("--target-north", type=float, default=0.0)
ap.add_argument("--tol", type=float, default=15.0, help="arcsec")
ap.add_argument("--arcsec-per-px", type=float, default=0.110, help="bin-1 scale")
ap.add_argument("--min-peak", type=float, default=20000.0, help="Saturn present only if the smoothed peak is this many ADU over the sky (saturated Saturn ~61000; no-Saturn frames <3400; a moon alone must not pass)")
ap.add_argument("--min-area", type=float, default=2000.0, help="...and its half-peak blob this many px (Saturn ~13600 at bin 2; a moon ~100)")
ap.add_argument("--lost", type=int, default=3, help="end the set after this many frames in a row without Saturn")
ap.add_argument("--npy", nargs="*", default=None, help="skip the capture, stack these")
a = ap.parse_args()
E = np.array([float(v) for v in a.east.split(",")]); E /= np.linalg.norm(E); N = np.array([-E[1], E[0]])
SC = a.arcsec_per_px * a.bin
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "telemetry", time.strftime("%Y-%m-%d"), "satmoons"); os.makedirs(OUT, exist_ok=True)
def log(s): print("%s  %s" % (time.strftime("%H:%M:%S"), s), flush=True)
def _term(signum, _f): raise SystemExit(128 + signum)      # satloop stops children with SIGTERM: finish the move (tracking on, 'none')
signal.signal(signal.SIGTERM, _term)
STAMP = time.strftime("%H%M%S")

def saturn(img):
    """Saturn's centroid, or None when it is not in the frame. Without the gate a lost planet made the
    brightest hot pixel 'Saturn' and the hold chased it several arcmin away (02:53, 2026-09-27)."""
    sm = ndimage.gaussian_filter(img, 2); med = np.median(img)
    if sm.max() - med < a.min_peak:
        return None
    lab, n = ndimage.label(sm > med + 0.5 * (sm.max() - med))
    areas = ndimage.sum(np.ones_like(sm), lab, range(1, n + 1))
    k = int(np.argmax(areas)) + 1
    if areas[k - 1] < a.min_area:
        return None
    cy, cx = ndimage.center_of_mass(np.ones_like(sm), lab, k)          # centroid of the saturated blob, not max()
    return np.array([cx, cy])

def move(e_as, n_as):
    from air_rpc import Air
    from daypipes import host
    def mdo(fn):
        m = Air(host(), 4400)
        try: return fn(m)
        finally: m.close()
    def pulse(cmd, secs):
        def f(m):
            idx = m.call("scope_get_info", [])["result"]["slew_rate_index"]
            try: m.call("scope_set_slew_rate", [4]); m.call("scope_move", [cmd]); time.sleep(secs)
            finally: m.call("scope_move", ["none"]); m.call("scope_move", ["none"]); m.call("scope_set_slew_rate", [idx])
        mdo(f)
    if abs(n_as) > 5: pulse("south" if n_as > 0 else "north", min(abs(n_as) / 312.0, 1.0))   # pier west: 'south' raises Dec
    if e_as > 5:
        try: mdo(lambda m: m.call("scope_set_track_state", [False])); time.sleep(min(e_as / 15.0, 10.0))
        finally: mdo(lambda m: m.call("scope_set_track_state", [True]))
    elif e_as < -5: pulse("west", min(-e_as / 312.0, 1.0))

files, times, LOST = [], [], False
if a.npy:
    files = sorted(a.npy)
else:
    from daypipes import Pipes
    p = Pipes(); stamp = STAMP
    try:
        p.setup("preview", a.exp, a.gain, a.bin); p.grab(timeout=40)
        miss = 0
        for i in range(a.frames):
            img, w, h, info = p.grab(timeout=40); t = time.time(); img = img.astype(np.float64)
            s = saturn(img)
            if s is None:
                miss += 1; LOST = miss >= a.lost
                log("frame %2d/%d: Saturn NOT in the frame (smoothed peak %.0f over the sky < %.0f) -- no correction, not stacked (%d in a row)%s"
                    % (i + 1, a.frames, ndimage.gaussian_filter(img, 2).max() - np.median(img), a.min_peak, miss, " -- ending the set" if LOST else ""))
                if LOST: break
                continue
            miss = 0
            d = (s - np.array([w / 2.0, h / 2.0])) * SC
            e_as, n_as = float(d @ E), float(d @ N)                  # where Saturn is, arcsec east/north of the frame centre
            fn = os.path.join(OUT, "%s_%03d_%gs_g%d_bin%d.npy" % (stamp, i, a.exp, a.gain, a.bin)); np.save(fn, img.astype(np.uint16)); files.append(fn); times.append(t)
            err_e, err_n = e_as - a.target_east, n_as - a.target_north
            log("frame %2d/%d: Saturn %+5.0f\" E %+5.0f\" N of centre (target %+.0f/%+.0f) ; sky %.0f ; max %.0f" % (i + 1, a.frames, e_as, n_as, a.target_east, a.target_north, np.median(img), img.max()))
            if abs(err_e) > a.tol or abs(err_n) > a.tol:
                move(err_e, err_n); log("   corrected: pointing moved %+.0f\" E %+.0f\" N" % (err_e, err_n)); p.grab(timeout=40)
    finally:
        try: p.close()
        except Exception: pass
# -- stack on Saturn ------------------------------------------------------------------------
imgs = [np.load(f).astype(np.float64) for f in files]
cs = [saturn(im) for im in imgs]
keep = [j for j, c in enumerate(cs) if c is not None]
if len(keep) < len(imgs):
    log("stack: Saturn in %d of %d frames; the others are left out" % (len(keep), len(imgs)))
if not keep:
    log("no frame with Saturn -- nothing to stack"); sys.exit(3)
if len(times) == len(imgs): times = [times[j] for j in keep]
imgs = [imgs[j] for j in keep]; cs = [cs[j] for j in keep]; ref = cs[len(cs) // 2]
stack = np.mean([ndimage.shift(im, (ref[1] - c[1], ref[0] - c[0]), order=1, mode="nearest") for im, c in zip(imgs, cs)], axis=0)
np.save(os.path.join(OUT, "%s_stack_%d.npy" % (STAMP, len(imgs))), stack.astype(np.float32))
json.dump(dict(saturn=[float(ref[0]), float(ref[1])], t_mid=(float(np.mean(times)) if times else None), n=len(imgs), bin=a.bin, exp=a.exp, gain=a.gain), open(os.path.join(OUT, "%s_stack_%d.json" % (STAMP, len(imgs))), "w"))
sky = np.median(stack); noise = 1.4826 * np.median(np.abs(stack - sky)) / 1.0
log("stacked %d frames on Saturn at (%.0f,%.0f) bin-%d px ; sky %.0f, noise %.1f" % (len(imgs), ref[0], ref[1], a.bin, sky, noise))
# -- Horizons ---------------------------------------------------------------------------------
tmid = dt.datetime.fromtimestamp(np.mean(times) if times else os.path.getmtime(files[len(files) // 2]), dt.timezone.utc)
def radec(cmd):
    t0 = tmid.strftime("%Y-%m-%d %H:%M"); t1 = (tmid + dt.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
    q = dict(format="text", COMMAND="'%s'" % cmd, EPHEM_TYPE="'OBSERVER'", CENTER="'coord@399'", COORD_TYPE="'GEODETIC'",
             SITE_COORD="'12.5553,55.6894,0.02'", START_TIME="'%s'" % t0, STOP_TIME="'%s'" % t1, STEP_SIZE="'1 m'",
             QUANTITIES="'1'", ANG_FORMAT="'DEG'", CSV_FORMAT="'YES'")
    txt = urllib.request.urlopen("https://ssd.jpl.nasa.gov/api/horizons.api?" + urllib.parse.urlencode(q), timeout=30).read().decode()
    nums = [float(v) for v in txt.split("$$SOE")[1].split("$$EOE")[0].strip().splitlines()[0].split(",") if re.fullmatch(r"\s*-?\d+\.\d+\s*", v)]
    return nums[0], nums[1]
names = {"601": "Mimas", "602": "Enceladus", "603": "Tethys", "604": "Dione", "605": "Rhea", "606": "Titan", "607": "Hyperion", "608": "Iapetus"}
marks = []
try:
    S = radec("699")
    for c, nm in names.items():
        ra, de = radec(c)
        off = np.array([(ra - S[0]) * 3600 * math.cos(math.radians(S[1])), (de - S[1]) * 3600])
        pix = ref + (off[0] * E + off[1] * N) / SC
        det = None
        if 0 <= pix[0] < stack.shape[1] and 0 <= pix[1] < stack.shape[0]:
            r = int(max(6, 3.0 / SC)); y0, x0 = int(pix[1]) - r, int(pix[0]) - r
            box = ndimage.gaussian_filter(stack, 1.5)[max(0, y0):y0 + 2 * r + 1, max(0, x0):x0 + 2 * r + 1]
            if box.size:
                j = np.unravel_index(int(np.argmax(box)), box.shape); pk = float(box.max() - sky)
                det = (max(0, x0) + j[1], max(0, y0) + j[0], pk / max(noise, 1e-6))
        marks.append((nm, off, pix, det))
        print("%-9s %+6.0f\" E %+5.0f\" N -> px (%6.0f,%6.0f) %s" % (nm, off[0], off[1], pix[0], pix[1],
              ("peak %5.1f sigma at (%d,%d), %.1f px from predicted" % (det[2], det[0], det[1], math.hypot(det[0] - pix[0], det[1] - pix[1]))) if det else "outside the frame"))
except Exception as e:
    log("Horizons failed: %s" % e)
# -- PNG ---------------------------------------------------------------------------------------
import cv2
x = np.clip(stack - sky, 0, None); v = np.arcsinh(x / (4 * max(noise, 1))) / np.arcsinh(np.percentile(x, 99.95) / (4 * max(noise, 1)))
im8 = cv2.cvtColor((np.clip(v, 0, 1) * 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
for nm, off, pix, det in marks:
    if det is None: continue
    ok = det[2] > 5 and math.hypot(det[0] - pix[0], det[1] - pix[1]) < 25
    col = (60, 160, 255) if ok else (140, 140, 140)
    cv2.circle(im8, (int(pix[0]), int(pix[1])), 22, col, 2, cv2.LINE_AA)
    cv2.putText(im8, nm, (int(pix[0]) + 26, int(pix[1]) + 8), cv2.FONT_HERSHEY_SIMPLEX, 0.9, col, 2, cv2.LINE_AA)
png = os.path.join(OUT, "%s_stack_%d_moons.png" % (STAMP, len(imgs))); cv2.imwrite(png, im8); log("wrote %s" % png)
if LOST:
    log("RESULT Saturn lost during the set (stack made from the frames before)"); sys.exit(2)
