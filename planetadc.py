#!/usr/bin/env python3
"""ADC check on a planet: offset of the blue image from the red one, split along / across the vertical.

Focus page, bin 1, 16-bit (the planet must be in the 1:1 centre crop -- run planetcentre.py first).
Band-passed R and B planes, masked to the planet, phase-correlated; G1/G2 as the control (should
read ~0). Vertical from the planet's parallactic angle and the camera angle (--east, camangle).
Blue ABOVE red along the vertical = under-corrected (spread the levers); BELOW = over-corrected.

    ASIAIR_HOST=192.168.1.36 python3 -u planetadc.py --east=0.997,0.079 --ra 0.776 --dec 2.06
"""
import argparse, datetime as dt, math, os, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daypipes import Pipes, log
from moonreg import bandpass, measure_shift
from moonephem import moon

ap = argparse.ArgumentParser()
ap.add_argument("--east", required=True, help="sky east on the sensor 'x,y' (camangle)")
ap.add_argument("--ra", type=float, required=True, help="planet RA, hours (J2000 is fine for q)")
ap.add_argument("--dec", type=float, required=True, help="planet Dec, degrees")
ap.add_argument("--frames", type=int, default=8)
ap.add_argument("--gain", type=int, default=250)
ap.add_argument("--coef", type=float, default=0.568, help="measured air dispersion B-R, arcsec per tan z (levers together, 2026-09-26)")
ap.add_argument("--arcsec-per-px", type=float, default=0.110)
ap.add_argument("--lat", type=float, default=55.689444); ap.add_argument("--lon", type=float, default=12.555278)
a = ap.parse_args()
E = np.array([float(v) for v in a.east.split(",")]); E /= np.linalg.norm(E); N = np.array([-E[1], E[0]])

p = Pipes(); exp = 0.1; rows = []
try:
    p.setup("focus", exp, a.gain, 1); p.grab(timeout=40)
    for it in range(5):
        img, w, h, _ = p.grab(timeout=40); pk = float(ndimage.uniform_filter(img.astype(float), 5).max())
        log("exposure %.0f ms -> smoothed peak %.0f" % (exp * 1000, pk))
        if 18000 < pk < 50000: break
        exp = float(np.clip(exp * 35000 / max(pk - 1000, 500), 0.002, 1.0)); p.c("set_control_value", ["Exposure", int(exp * 1e6)]); p.exp = exp; time.sleep(0.3); p.grab(timeout=40)
    for i in range(a.frames):
        img, w, h, _ = p.grab(timeout=40); img = img.astype(np.float64)
        pl = {k: img[dy::2, dx::2] for k, (dy, dx) in dict(R=(0, 0), G1=(0, 1), G2=(1, 0), B=(1, 1)).items()}
        g = (pl["G1"] + pl["G2"]) / 2; sm = ndimage.gaussian_filter(g, 2); bg = np.median(g)
        cy, cx = np.unravel_index(int(np.argmax(sm)), sm.shape); H = 220
        y0, y1, x0, x1 = max(0, cy - H), min(g.shape[0], cy + H), max(0, cx - H), min(g.shape[1], cx + H)
        mask = ndimage.gaussian_filter(ndimage.binary_dilation(sm[y0:y1, x0:x1] > bg + 0.08 * (sm.max() - bg), iterations=12).astype(float), 4)
        pr = {k: bandpass(v[y0:y1, x0:x1], 1.2, 20) * mask for k, v in pl.items()}
        rb = measure_shift(pr["R"], pr["B"], prepped=True); gg = measure_shift(pr["G1"], pr["G2"], prepped=True)
        brx, bry = (rb[0] + 0.5) * 2, (rb[1] + 0.5) * 2; gx, gy = gg[0] - 0.5, gg[1] + 0.5
        rows.append((brx, bry))
        print("frame %d: planet at crop (%d,%d)  B-R %+.2f %+.2f sensor px (peak %.3f) ; G2-G1 control %+.2f %+.2f plane px" % (i, 2 * cx, 2 * cy, brx, bry, rb[2], gx, gy), flush=True)
finally:
    p.close()
med = np.median(np.array(rows), axis=0); sc = a.arcsec_per_px
mm = moon(dt.datetime.now(dt.timezone.utc), a.lat, a.lon); Hr = math.radians((mm["lst_h"] - a.ra) * 15)
d, ph = math.radians(a.dec), math.radians(a.lat)
q = math.degrees(math.atan2(math.sin(Hr), math.tan(ph) * math.cos(d) - math.sin(d) * math.cos(Hr)))
alt = math.degrees(math.asin(math.sin(ph) * math.sin(d) + math.cos(ph) * math.cos(d) * math.cos(Hr)))
up = math.sin(math.radians(q)) * E + math.cos(math.radians(q)) * N; up /= np.linalg.norm(up)
along = float(med @ up) * sc; across = float(up[0] * med[1] - up[1] * med[0]) * sc
air = a.coef * math.tan(math.radians(90 - alt))
print("MEDIAN B-R %+.2f %+.2f sensor px ; planet alt %.1f q %+.1f ; zenith on the sensor (%+.2f,%+.2f)" % (med[0], med[1], alt, q, up[0], up[1]))
print("RESULT blue %s red by %.2f\" along the vertical, %.2f\" across ; the air alone would give %.2f\" -> the ADC removes %.2f\" (%.0f%%)" % (
    "ABOVE" if along > 0 else "BELOW", abs(along), abs(across), air, air - along, 100 * (air - along) / air))
print("ADVICE: %s" % ("well set (|along| < 0.15\")" if abs(along) < 0.15 else ("spread the levers a little (equal, opposite)" if along > 0 else "bring the levers a little together (equal amounts)")))
