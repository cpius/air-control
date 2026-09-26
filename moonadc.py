#!/usr/bin/env python3
"""ADC check on the Moon: offset of the blue image relative to the red one,
from the Bayer planes of a 16-bit RGGB frame by phase correlation of the
high-passed lunar texture. The G1/G2 pair is measured the same way as a
control -- geometrically they are offset by exactly (+0.5, -0.5) plane px,
so the method's error is visible in that number.

    python3 moonadc.py --npy telemetry/2026-09-24/moonlook/*adc*.npy --arcsec-per-px 0.144 --alt 20.8 --q -45 --jacobian=-23,299.8,-237.5,27.5

Reports the R-B vector in sensor (bin-1) px and arcsec, its component along
the predicted vertical (from the parallactic angle q and the camera Jacobian)
and the uncorrected expectation 0.53" x tan(z) (R 620 nm vs B 470 nm, sea
level, 10 C). Blue is refracted MORE, so without an ADC the blue image sits
ABOVE the red one (toward the zenith): R-B points AWAY from the zenith.
"""
import argparse, glob, math, os, sys
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from moonreg import bandpass, measure_shift

ap = argparse.ArgumentParser()
ap.add_argument("--npy", nargs="+", default=None, help="saved frames to analyse (or use --live)")
ap.add_argument("--live", action="store_true", help="take --frames focus-page bin-1 frames now (ASIAIR_HOST) and analyse them")
ap.add_argument("--frames", type=int, default=4); ap.add_argument("--exp-ms", type=float, default=100.0); ap.add_argument("--gain", type=int, default=100)
ap.add_argument("--arcsec-per-px", type=float, default=0.110, help="bin-1 sensor scale (barlow+ADC: 0.110 measured 2026-09-24 from the sidereal->lunar rate step)")
ap.add_argument("--lam-r", type=float, default=660.0, help="effective red wavelength, nm"); ap.add_argument("--lam-b", type=float, default=470.0)
ap.add_argument("--temp-c", type=float, default=10.0); ap.add_argument("--press-hpa", type=float, default=1013.0)
ap.add_argument("--lat", type=float, default=55.689444); ap.add_argument("--lon", type=float, default=12.555278)
ap.add_argument("--bin", type=int, default=1, help="binning of the frame (plane px = 2*bin sensor px)")
ap.add_argument("--alt", type=float, default=None, help="altitude of the Moon, deg (default: from the ephemeris now)")
ap.add_argument("--q", type=float, default=None, help="parallactic angle, deg (default: from the ephemeris now)")
ap.add_argument("--jacobian", default=None, help="bin-2 px per arcmin: dx/dRA,dx/dDec,dy/dRA,dy/dDec (star shift per register move)")
ap.add_argument("--east", default=None, help="sky east on the sensor 'x,y', measured tonight by ratestep.py -- overrides the Jacobian's camera angle (it keeps its parity); the camera turns whenever the ADC is re-levelled")
ap.add_argument("--pattern", default="RGGB")
ap.add_argument("--lo", type=float, default=2.0, help="smoothing sigma (plane px)"); ap.add_argument("--hi", type=float, default=30.0, help="high-pass sigma (plane px)")
ap.add_argument("--tiles", type=int, default=3, help="tiles per axis for a per-region check")
ap.add_argument("--page", default="focus", choices=["focus", "preview"], help="focus = 1:1 centre crop (fast); preview = whole sensor (more disc when the limb is in the crop)")
ap.add_argument("--sky-frac", type=float, default=0.2, help="pixels below this fraction of the disc level (smoothed) are sky and get zero weight; 0 = no mask")
a = ap.parse_args()

def planes(img):
    pat = a.pattern.upper(); out = {}
    for k, (dy, dx) in zip(pat, [(0, 0), (0, 1), (1, 0), (1, 1)]):
        out.setdefault(k, []).append(img[dy::2, dx::2].astype(np.float64))
    return out["R"][0], out["G"][0], out["G"][1], out["B"][0]

def prep(pl):
    return bandpass(pl, a.lo, a.hi)

def shift(refp, movp):
    return measure_shift(refp, movp, prepped=True)

def sky_mask(G):
    """Soft weight: the lunar disc plus ~hi px of sky beyond the limb (the limb is the sharpest
    feature, keep it). bandpass() contrast-normalises, so empty sky otherwise comes back as
    full-weight noise -- on a limb field (2026-09-26) that broke the G1/G2 control by 5 plane px."""
    sm = ndimage.gaussian_filter(G, a.hi)
    disc = sm > a.sky_frac * np.percentile(sm, 99)
    if a.sky_frac <= 0 or disc.mean() > 0.98:
        return np.ones_like(G), disc
    wt = ndimage.gaussian_filter(ndimage.binary_dilation(disc, iterations=int(a.hi)).astype(np.float64), a.hi / 3)
    return wt, disc

def measure(img):
    R, G1, G2, B = planes(img)
    wt, disc = sky_mask((G1 + G2) / 2)
    pr, pg1, pg2, pb = (prep(p) * wt for p in (R, G1, G2, B))
    rb = shift(pr, pb); gg = shift(pg1, pg2)
    out = dict(rb=rb, gg=gg, tiles=[], disc=float(disc.mean()))
    h, w = R.shape; n = a.tiles
    for i in range(n):
        for j in range(n):
            ys, xs = slice(i * h // n, (i + 1) * h // n), slice(j * w // n, (j + 1) * w // n)
            if disc[ys, xs].mean() < 0.5:
                continue                                                    # mostly sky: nothing to register
            out["tiles"].append((i, j) + shift(prep(R[ys, xs]) * wt[ys, xs], prep(B[ys, xs]) * wt[ys, xs])[:2])
    return out

def n_minus_1(lam_nm):
    s2 = (1000.0 / lam_nm) ** 2
    ns = (8342.54 + 2406147.0 / (130.0 - s2) + 15998.0 / (38.9 - s2)) * 1e-8          # Edlen 1966, 15 C 1013.25 hPa
    return ns * (a.press_hpa / 1013.25) * (288.15 / (273.15 + a.temp_c))
DISP_COEF = (n_minus_1(a.lam_b) - n_minus_1(a.lam_r)) * 206264.8                        # arcsec per tan(z)

if a.alt is None or a.q is None:
    import datetime as dt
    from moonephem import moon
    mm = moon(dt.datetime.now(dt.timezone.utc), a.lat, a.lon)
    if a.alt is None: a.alt = mm["alt"]
    if a.q is None: a.q = mm["q"]
    print("ephemeris now: Moon alt %.2f az %.2f  parallactic angle q %+.1f deg" % (mm["alt"], mm["az"], mm["q"]))

if a.live:
    import time
    from daypipes import Pipes, log
    outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "telemetry", time.strftime("%Y-%m-%d"), "moonlook")
    os.makedirs(outdir, exist_ok=True)
    p = Pipes(); files = []
    try:
        exp = a.exp_ms / 1000.0
        p.setup(a.page, exp, a.gain, 1); p.grab(timeout=40)
        for it in range(6):
            img, w, h, info = p.grab(timeout=40); p995 = float(np.percentile(img, 99.5))
            log("auto %d: %.1f ms -> p99.5 %.0f" % (it, exp * 1000, p995))
            if 22000 <= p995 <= 50000: break
            exp = float(np.clip(exp * (36000 - 1000) / max(p995 - 1000, 50), 0.0002, 1.0))
            p.c("set_control_value", ["Exposure", int(round(exp * 1e6))]); p.exp = exp; p.s.air.drain_events(); time.sleep(0.3); p.grab(timeout=40)
        for i in range(a.frames):
            img, w, h, info = p.grab(timeout=40)
            # frame index in the name: 100 ms frames arrive twice a second and HHMMSS alone overwrote every other one
            fn = os.path.join(outdir, "%s_%02d_adclive_%s_bin1_%gms_g%d.npy" % (time.strftime("%H%M%S"), i, a.page, round(exp * 1000, 1), a.gain))
            np.save(fn, img.astype(np.uint16)); files.append(fn)
            log("frame %d: %dx%d fresh=%s p99.5 %.0f max %.0f -> %s" % (i, w, h, info.get("fresh"), np.percentile(img, 99.5), img.max(), os.path.basename(fn)))
    finally:
        try: p.close()
        except Exception: pass
else:
    files = sorted(sum([glob.glob(f) for f in (a.npy or [])], []))
if not files:
    sys.exit("no files (give --npy or --live)")
sc = a.arcsec_per_px * a.bin
rows = []
for f in files:
    img = np.load(f).astype(np.float64)
    m = measure(img)
    # geometric offsets: B plane sits (+1,+1) sensor px from R; G2 sits (-1,+1) from G1 -> features appear at (-0.5,-0.5) / (+0.5,-0.5) plane px
    gx, gy = m["gg"][0] - 0.5, m["gg"][1] + 0.5
    rbx, rby = (m["rb"][0] + 0.5) * 2, (m["rb"][1] + 0.5) * 2            # sensor px, geometry removed
    tiles = np.array([[(t[2] + 0.5) * 2, (t[3] + 0.5) * 2] for t in m["tiles"]]).reshape(-1, 2)
    rows.append((rbx, rby))
    print("%s: B-minus-R offset dx %+.2f dy %+.2f sensor px = %+.2f\" %+.2f\" |%.2f\"| ; disc %.0f%% of field, %d/%d tiles on it, sd %.2f/%.2f px ; G2-G1 control residual %+.2f %+.2f plane px (peak %.3f)" % (
        os.path.basename(f), rbx, rby, rbx * sc, rby * sc, math.hypot(rbx, rby) * sc, 100 * m["disc"], len(tiles), a.tiles ** 2,
        tiles[:, 0].std() if len(tiles) else float("nan"), tiles[:, 1].std() if len(tiles) else float("nan"), gx, gy, m["gg"][2]))
    for t in m["tiles"]:
        print("    tile row %d col %d: B-R dx %+.2f dy %+.2f sensor px" % (t[0], t[1], (t[2] + 0.5) * 2, (t[3] + 0.5) * 2))
rows = np.array(rows); med = np.median(rows, axis=0)
print("MEDIAN B-R: dx %+.2f dy %+.2f sensor px = %+.2f\" %+.2f\"  |%.2f\"|  (+x right, +y down; B relative to R)" % (med[0], med[1], med[0] * sc, med[1] * sc, math.hypot(*med) * sc))
if a.alt is not None:
    z = 90 - a.alt
    exp = DISP_COEF * math.tan(math.radians(z))
    print("expected uncorrected B-R dispersion at alt %.1f (z %.1f): %.2f\" = %.1f sensor px  (%.0f vs %.0f nm, %.0f hPa, %.0f C: %.3f\"/tan z)" % (a.alt, z, exp, exp / sc, a.lam_r, a.lam_b, a.press_hpa, a.temp_c, DISP_COEF))
    if a.q is not None and a.jacobian:
        J = np.array([float(v) for v in a.jacobian.split(",")]).reshape(2, 2)
        # a register move of +1' east shifts stars by J[:,0]; a sky direction east therefore points along -J[:,0] on the sensor
        east = -J[:, 0] / np.linalg.norm(J[:, 0]); north = -J[:, 1] / np.linalg.norm(J[:, 1])
        if a.east:
            # measured tonight (ratestep.py); the Jacobian then only supplies the image parity, which a camera rotation keeps
            parity = np.sign(east[0] * north[1] - east[1] * north[0])
            east = np.array([float(v) for v in a.east.split(",")]); east /= np.linalg.norm(east)
            north = parity * np.array([-east[1], east[0]])
            print("sky east on the sensor from --east: (%+.3f, %+.3f), north (%+.3f, %+.3f)" % (east[0], east[1], north[0], north[1]))
        up = math.sin(math.radians(a.q)) * east + math.cos(math.radians(a.q)) * north
        up /= np.linalg.norm(up)
        along = float(np.dot(med, up)) * sc; perp = float(np.hypot(*(med - np.dot(med, up) * up))) * sc
        print("zenith direction on the sensor (from q %+.1f and the Jacobian): (%+.2f, %+.2f)" % (a.q, up[0], up[1]))
        print("B-R component toward the zenith: %+.2f\"  (uncorrected would be %+.2f\": blue higher) ; perpendicular |%.2f\"|" % (along, exp, perp))
        frac = along / exp if exp else float("nan")
        verdict = "ADC is doing nothing" if abs(frac - 1) < 0.25 else ("ADC over-corrected (blue below red)" if frac < -0.25 else ("ADC well set" if abs(frac) < 0.25 else "ADC partly correcting (%.0f%% of the dispersion remains)" % (100 * frac)))
        if perp > 0.5 * exp and abs(along) < 0.5 * exp:
            verdict += " -- but the residual is mostly PERPENDICULAR to the vertical: the ADC's orientation (roll) is off, or the assumed camera angle is wrong"
        print("VERDICT: %s" % verdict)
        # what the ADC itself is doing: c = measured - atmosphere (as B-R vectors); its angle from the vertical is the roll error
        cvec = med * sc - exp * up
        ang = math.degrees(math.atan2(float(np.dot(cvec, np.array([-up[1], up[0]]))), float(np.dot(cvec, -up))))
        print("ADC correction in effect: %.2f\" (needs %.2f\": x%.1f), axis %+.0f deg from the vertical (0 = pushing blue straight down); residual dispersion %.2f\" = %.1f sensor px" % (
            float(np.hypot(*cvec)), exp, exp / max(float(np.hypot(*cvec)), 1e-6), ang, float(np.hypot(*med)) * sc, float(np.hypot(*med))))
