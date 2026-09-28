#!/usr/bin/env python3
"""Find a bright planet near the current pointing with the 7'x4' barlow+ADC field, when the
register cannot be trusted (e.g. after a power cut reset it to 'home').

Defocus hard (--eaf-search) so the planet becomes a disc several arcmin across -- it is then
caught whenever it is within ~half a disc of the frame, 6-8x the sky per frame -- and spiral
outward from where we are with 20x joystick pulses. Positions come from the mount register
after every move (encoder-accurate even when its absolute zero is wrong), so pulse errors do
not accumulate. Detector: tile medians against the FIRST frame's tile medians, fixed threshold
(a disc triples the frame noise, so a self-scaled sigma would hide it -- 2026-09-16).
Found -> report sensor position and the register offset; not found -> back to the start.
EAF always goes back to --eaf-focus; tracking always left on.

    ASIAIR_HOST=192.168.1.36 EAF_MIN=40000 EAF_MAX=70000 python3 -u planetary/planetsearch.py --eaf-focus 60250 --eaf-search 45250 --rings 4
"""
import argparse, json, math, os, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

ap = argparse.ArgumentParser()
ap.add_argument("--eaf-focus", type=int, required=True, help="EAF position to return to")
ap.add_argument("--eaf-search", type=int, required=True, help="EAF position for the search (defocused)")
ap.add_argument("--rings", type=int, default=4, help="spiral rings (ring r has 8r frames)")
ap.add_argument("--step-ra", type=float, default=9.0, help="arcmin between columns (E-W; the field's 4' side)")
ap.add_argument("--step-dec", type=float, default=12.0, help="arcmin between rows (N-S; the field's 7' side)")
ap.add_argument("--rate-arcmin-s", type=float, default=5.2, help="20x joystick speed (RIG.md: 5.1-5.46)")
ap.add_argument("--exp", type=float, default=1.0); ap.add_argument("--gain", type=int, default=300); ap.add_argument("--bin", type=int, default=2)
ap.add_argument("--jump", type=float, default=0.30, help="a tile brighter than the first frame's by this fraction of the SKY level = planet")
ap.add_argument("--true-dec", type=float, default=2.0, help="approximate real Dec, for RA arcmin (cos Dec)")
ap.add_argument("--east", default="0.239,0.971", help="sky east on the sensor (camangle.py)")
ap.add_argument("--centre", action="store_true", help="after a find, put the donut centre in the frame centre (known --disc-radius), then refocus")
ap.add_argument("--disc-radius", type=float, default=2295.0, help="outer radius of the search donut, sensor px (8.4' disc at EAF -15000, 2026-09-26)")
ap.add_argument("--centre-tol", type=float, default=35.0, help="arcsec")
ap.add_argument("--arcsec-per-px", type=float, default=0.110, help="--centre: bin-1 scale (0.1866 for the 2026-09-27 f/15.7 lens-cell train)")
a = ap.parse_args()

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "..", "telemetry", time.strftime("%Y-%m-%d"), "planetsearch"); os.makedirs(OUT, exist_ok=True)
T0 = time.time()
def log(s): print("%s %+6.0fs  %s" % (time.strftime("%H:%M:%S"), time.time() - T0, s), flush=True)
def res(r): return r.get("result", r) if isinstance(r, dict) else r

from air_rpc import Air
def mcall(fn):
    m = Air(os.environ["ASIAIR_HOST"], 4400)          # fresh socket: 4400 drops idle ones
    try: return fn(m)
    finally: m.close()
def reg():
    s = mcall(lambda m: res(m.call("scope_get_info", [])))
    return s
COSD = math.cos(math.radians(a.true_dec))
def reg_xy(s, s0):
    """register offset from the start, arcmin: (RA toward east on the sky, Dec)"""
    dra = ((s["RA"] - s0["RA"] + 12) % 24 - 12) * 900.0 * COSD
    return dra, (s["Dec"] - s0["Dec"]) * 60.0

DIRS = {}                                            # which command moves which way (learned)
def pulse(cmd, secs):
    secs = float(np.clip(secs, 0.15, 8.0))
    def f(m):
        idx = res(m.call("scope_get_info", []))["slew_rate_index"]
        try:
            # [dir, whole seconds] = mount-side dead-man timer: a lost "none" can't run the mount away
            m.call("scope_set_slew_rate", [4]); m.call("scope_move", [cmd, int(math.ceil(secs))]); time.sleep(secs)
        finally:
            m.call("scope_move", ["none"]); m.call("scope_set_slew_rate", [idx])
    mcall(f); time.sleep(1.2)

def move_to(target, s0, tol=1.5, tries=3):
    """drive the register to target (dRA', dDec') relative to s0, one axis at a time"""
    for _ in range(tries):
        s = reg(); x, y = reg_xy(s, s0); ex, ey = target[0] - x, target[1] - y
        if abs(ex) <= tol and abs(ey) <= tol: return s
        for axis, err in (("ra", ex), ("dec", ey)):
            if abs(err) <= tol: continue
            want = "+" if err > 0 else "-"
            cmd = DIRS.get((axis, want))
            if cmd is None:                                   # learn: try one, check the sign
                cmd = {"ra": "east", "dec": "north"}[axis] if want == "+" else {"ra": "west", "dec": "south"}[axis]
            b = reg(); bx, by = reg_xy(b, s0)
            pulse(cmd, abs(err) / a.rate_arcmin_s)
            e = reg(); exx, eyy = reg_xy(e, s0); d = (exx - bx) if axis == "ra" else (eyy - by)
            got = "+" if d > 0 else "-"
            DIRS[(axis, got)] = cmd
            other = {"east": "west", "west": "east", "north": "south", "south": "north"}[cmd]
            DIRS[(axis, "-" if got == "+" else "+")] = other
            log("   %s %.2fs -> %s %+.1f'  (wanted %+.1f')" % (cmd, abs(err) / a.rate_arcmin_s, axis, d, err))
    return reg()

def spiral(n):
    pts = [(0, 0)]; x = y = 0
    for r in range(1, n + 1):
        x, y = r, -r + 1
        pts.append((x, y))
        for _ in range(2 * r - 1): y += 1; pts.append((x, y))
        for _ in range(2 * r): x -= 1; pts.append((x, y))
        for _ in range(2 * r): y -= 1; pts.append((x, y))
        for _ in range(2 * r): x += 1; pts.append((x, y))
    return pts

from daypipes import Pipes, move_to as eaf_move, focuser_pos
p = Pipes(); found = None; s0 = None
try:
    s0 = reg(); log("start register RA %.5f Dec %.4f  track %s  pier %s" % (s0["RA"], s0["Dec"], s0["is_enable_track"], s0["pier_side"]))
    p.setup("preview", a.exp, a.gain, a.bin); p.grab(timeout=40)
    log("EAF %d -> %d (defocus for the search)" % (focuser_pos(p), a.eaf_search)); eaf_move(p, a.eaf_search)
    base = None; bias = 3968.0 if a.bin == 2 else 3900.0
    pts = spiral(a.rings); log("%d frames, steps RA %.0f' Dec %.0f'" % (len(pts), a.step_ra, a.step_dec))
    for k, (i, j) in enumerate(pts):
        tgt = (i * a.step_ra, j * a.step_dec)
        if k: s = move_to(tgt, s0)
        else: s = s0
        x, y = reg_xy(s, s0)
        img, w, h, info = p.grab(timeout=40)
        img = img.astype(np.float64)
        th, tw = h // 6, w // 8
        tiles = np.array([[np.median(img[r * th:(r + 1) * th, c * tw:(c + 1) * tw]) for c in range(8)] for r in range(6)])
        if base is None:
            base = tiles.copy(); sky = float(np.median(base) - bias); floors = []
            P = (base - bias) / max(sky, 30.0)                    # the frame's own shape: vignetting + moonlight gradient
            log("baseline sky %.0f ADU over bias (%.0f ADU/s)" % (sky, sky / a.exp))
        tn = (tiles - bias) / np.maximum(P, 0.2)                  # flattened tile levels, ADU over bias
        floor = float(np.percentile(tn, 10))
        ref = float(np.median(floors[-3:])) if floors else sky
        partial = (tn.max() - floor) / ref; full = (floor - ref) / ref
        jump = np.array([[max(partial, full)]])
        floors.append(floor)
        np.save(os.path.join(OUT, "%s_%03d.npy" % (time.strftime("%H%M%S"), k)), img[::2, ::2].astype(np.uint16))
        log("frame %3d grid (%+d,%+d) register %+6.1f' RA %+6.1f' Dec : sky %4.0f  brightest tile %+.2f  whole frame %+.2f  (max px %.0f)%s" % (
            k, i, j, x, y, floor, partial, full, img.max(), "   <== PLANET?" if jump.max() > a.jump else ""))
        if jump.max() > a.jump:
            r, c = np.unravel_index(int(np.argmax(tn)), tn.shape)
            sm = ndimage.gaussian_filter(img, 8); lab, n = ndimage.label(sm > np.percentile(img, 10) + 0.5 * a.jump * ref)
            kk = int(np.argmax(ndimage.sum(np.ones_like(sm), lab, range(1, n + 1)))) + 1 if n else 0
            cy, cx = ndimage.center_of_mass(sm, lab, kk) if kk else ((r + 0.5) * th, (c + 0.5) * tw)
            found = dict(frame=k, grid=(i, j), reg_offset=(x, y), sensor=(cx * a.bin, cy * a.bin), tile=(int(r), int(c)), jump=float(jump.max()))
            log("FOUND: bright disc around sensor (%.0f, %.0f) of 3840x2160, register offset RA %+.1f' Dec %+.1f'" % (cx * a.bin, cy * a.bin, x, y))
            if a.centre:
                from donutcentre import centre as dcentre
                E = np.array([float(v) for v in a.east.split(",")]); E /= np.linalg.norm(E); N = np.array([-E[1], E[0]])
                cur = img
                for it in range(3):
                    dx, dy, q = dcentre(cur, a.bin, a.disc_radius)
                    d = np.array([dx - 1920.0, dy - 1080.0]); e_as, n_as = float(d @ E) * a.arcsec_per_px, float(d @ N) * a.arcsec_per_px
                    log("  donut centre at sensor (%.0f, %.0f) (match %.2f): %+.0f\" east %+.0f\" north of the frame centre" % (dx, dy, q, e_as, n_as))
                    if abs(e_as) < a.centre_tol and abs(n_as) < a.centre_tol:
                        found["centred"] = True; break
                    if abs(n_as) > 10:
                        pulse("south" if n_as > 0 else "north", abs(n_as) / (a.rate_arcmin_s * 60))
                    if e_as > 10:
                        try: mcall(lambda m: m.call("scope_set_track_state", [False])); time.sleep(min(e_as / 15.0, 20.0))
                        finally: mcall(lambda m: m.call("scope_set_track_state", [True]))
                        time.sleep(0.8)
                    elif e_as < -10:
                        pulse("west", -e_as / (a.rate_arcmin_s * 60))
                    cur, *_ = p.grab(timeout=40); cur = cur.astype(np.float64)
                np.save(os.path.join(OUT, "%s_centred.npy" % time.strftime("%H%M%S")), cur[::2, ::2].astype(np.uint16))
            break
finally:
    try:
        if found is None and s0 is not None:
            log("not found -> back to the start"); move_to((0.0, 0.0), s0)
        log("EAF back to %d" % a.eaf_focus); eaf_move(p, a.eaf_focus)
    except Exception as e:
        log("!! cleanup failed: %s" % e)
    try: p.close()
    except Exception: pass
    try:
        st = mcall(lambda m: (m.call("scope_move", ["none"]), res(m.call("scope_get_track_state", []))))[1]
        if st is not True: mcall(lambda m: m.call("scope_set_track_state", [True])); log("tracking re-enabled")
        log("tracking %s" % mcall(lambda m: res(m.call("scope_get_track_state", []))))
    except Exception as e:
        log("!! could not confirm tracking (%s) -- CHECK THE MOUNT" % e)
print(json.dumps(dict(found=found, dirs={"%s%s" % k: v for k, v in DIRS.items()})))
