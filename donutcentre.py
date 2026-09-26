#!/usr/bin/env python3
"""Centre of a big defocused (SCT) donut from a frame that may show only part of it: correlate a
+1 annulus / -1 hole template of KNOWN outer radius with (+1 bright, -1 dark, 0 outside the frame).
The centre may lie outside the frame. Returns sensor px.

    python3 donutcentre.py --npy telemetry/2026-09-26/planetsearch/213640_008.npy --sensor-per-px 4 --radius-sensor 2295
"""
import argparse
import numpy as np
from scipy import ndimage

def centre(img, sensor_per_px, radius_sensor, hole=0.34, work=480):
    img = np.asarray(img, float)
    f = img.shape[1] / float(work)                               # downsample to ~work px wide
    small = ndimage.zoom(ndimage.gaussian_filter(img, max(f / 2, 1)), 1.0 / f, order=1)
    sky = np.percentile(small, 20); top = np.percentile(small, 99)
    val = np.where(small > sky + 0.35 * (top - sky), 1.0, -1.0)
    R = radius_sensor / (sensor_per_px * f)
    Ri = int(np.ceil(R)); pad = Ri + 2
    P = np.zeros((val.shape[0] + 2 * pad, val.shape[1] + 2 * pad)); P[pad:pad + val.shape[0], pad:pad + val.shape[1]] = val
    yy, xx = np.mgrid[-Ri:Ri + 1, -Ri:Ri + 1]; rr = np.hypot(yy, xx)
    T = np.where(rr <= R, 1.0, 0.0) - np.where(rr <= hole * R, 2.0, 0.0)          # annulus +1, hole -1
    Fp = np.fft.rfft2(P); Ft = np.fft.rfft2(T[::-1, ::-1], s=P.shape)
    C = np.fft.irfft2(Fp * Ft, s=P.shape)
    iy, ix = np.unravel_index(int(np.argmax(C)), C.shape)
    cy, cx = iy - Ri - pad, ix - Ri - pad                                           # template centre in small-frame px
    return cx * f * sensor_per_px, cy * f * sensor_per_px, float(C.max() / (T > 0).sum())

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--npy", required=True); ap.add_argument("--sensor-per-px", type=float, required=True)
    ap.add_argument("--radius-sensor", type=float, default=2295.0)
    a = ap.parse_args()
    x, y, q = centre(np.load(a.npy), a.sensor_per_px, a.radius_sensor)
    print("donut centre at sensor (%.0f, %.0f), match %.2f" % (x, y, q))
