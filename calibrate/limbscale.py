#!/usr/bin/env python3
"""Plate scale from the curvature of the Moon's BRIGHT limb (independent check
on driftscale.py).

    ASIAIR_HOST=192.168.1.36 python3 -u calibrate/limbscale.py --frames 8 --exp-ms 10 --gain 220 --east=0.821,0.572

Full-sensor bin-1 preview frames. Per frame: edge points by scanning rows and
columns from the sky side (never a mare boundary), a first circle, then radial
profiles from that centre with the 50% crossing between the LOCAL sky and the
LOCAL surface level (albedo varies along the limb), and a geometric circle fit
with MAD outlier rejection. The radius is FREE -- that is the measurement.

The apparent limb is not a circle: refraction squeezes it vertically by
dR/dh (~0.3% at alt 16). So the expected radius of curvature is computed for
the SAME arc: limb points at the arc's position angles are refracted,
projected on a tangent plane and circle-fitted, in arcsec. scale = that / px.
--east gives sky-east on the sensor (x right, y down) from driftscale.py; north
is east turned +90 deg in that frame (checked with 1' gotos 2026-09-27).

Limb topography (+-1-2 km = 0.5-1") is the dominant error on an arc of 30-45
deg: fit two separate stretches of the limb and compare.
"""
import argparse, datetime as dt, json, math, os, sys, time
import numpy as np
from scipy import ndimage, optimize
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import moonephem as me

ap = argparse.ArgumentParser()
ap.add_argument("--frames", type=int, default=8)
ap.add_argument("--exp-ms", type=float, default=10.0); ap.add_argument("--gain", type=int, default=220)
ap.add_argument("--east", default="0.821,0.572", help="sky-east unit vector on the sensor (x right, y down)")
ap.add_argument("--lat", type=float, default=55.689444); ap.add_argument("--lon", type=float, default=12.555278)
ap.add_argument("--temp-c", type=float, default=12.0); ap.add_argument("--pressure-hpa", type=float, default=1013.0)
ap.add_argument("--pixel-um", type=float, default=2.9)
ap.add_argument("--analyse", default=None, help="re-analyse a saved .npz")
ap.add_argument("--tag", default="limb")
ap.add_argument("--outdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "telemetry", time.strftime("%Y-%m-%d"), "limbscale"))
a = ap.parse_args() if __name__ == "__main__" else ap.parse_args([])
os.makedirs(a.outdir, exist_ok=True)
D2R = math.pi / 180
E = np.array([float(v) for v in a.east.split(",")]); E /= np.linalg.norm(E)
N = np.array([-E[1], E[0]])


def log(s):
    print("%s  %s" % (time.strftime("%H:%M:%S"), s), flush=True)


def green(img):
    img = img.astype(np.float32)
    return 0.5 * (img[0::2, 1::2] + img[1::2, 0::2])


def sky_side_points(g):
    sky = float(np.percentile(g, 2)); plat = float(np.percentile(g, 97)); C = plat - sky
    thr = sky + 0.5 * C; skylim = sky + 0.1 * C
    pts = []
    for axis in (0, 1):
        L = g if axis == 0 else g.T
        for i, prof in enumerate(L):
            for rev in (False, True):
                pr = prof[::-1] if rev else prof
                if pr[0] > skylim or pr[1] > skylim:
                    continue
                k = int(np.argmax(pr > thr))
                if k == 0 or pr[k] <= thr:
                    continue
                f = k - 1 + (thr - pr[k - 1]) / (pr[k] - pr[k - 1])
                j = len(pr) - 1 - f if rev else f
                pts.append((j, i) if axis == 0 else (i, j))
    return np.array(pts, dtype=float)


def fit_circle(x, y, c0=None):
    if c0 is None:                                        # Kasa algebraic start
        A = np.column_stack([x, y, np.ones_like(x)]); b = x * x + y * y
        cx, cy, c = np.linalg.lstsq(A, b, rcond=None)[0]
        c0 = (cx / 2, cy / 2, math.sqrt(c + cx * cx / 4 + cy * cy / 4))
    r = optimize.least_squares(lambda p: np.hypot(x - p[0], y - p[1]) - p[2], c0)
    return r.x


def radial_points(g, cx, cy, R, half=120.0, step=0.25):
    H, W = g.shape
    # angles whose limb point lies inside the frame (with margin)
    phis = np.linspace(-math.pi, math.pi, 7200, endpoint=False)
    lx = cx + R * np.cos(phis); ly = cy + R * np.sin(phis)
    ok = (lx > 8) & (lx < W - 8) & (ly > 8) & (ly < H - 8)
    phis = phis[ok]
    rr = np.arange(R - half, R + half, step)
    out = []
    for ph in phis[::2]:
        xs = cx + rr * math.cos(ph); ys = cy + rr * math.sin(ph)
        inside = (xs >= 1) & (xs < W - 2) & (ys >= 1) & (ys < H - 2)
        if inside.mean() < 0.9:
            continue
        prof = ndimage.map_coordinates(g, [ys, xs], order=1, mode="nearest")
        skyv = float(np.median(prof[-int(40 / step):]))          # outermost 40 sp
        surf = float(np.median(prof[int(10 / step):int(50 / step)]))   # 10-50 sp inside the window start... refined below
        # local surface level: 15-45 sp inside the first-pass crossing
        mid = skyv + 0.5 * (surf - skyv)
        k = np.nonzero(prof[::-1] > mid)[0]
        if not k.size:
            continue
        kc = len(prof) - 1 - k[0]
        i0 = max(kc - int(45 / step), 0); i1 = max(kc - int(15 / step), 1)
        surf = float(np.median(prof[i0:i1])) if i1 > i0 else surf
        if surf - skyv < 1500:
            continue
        mid = skyv + 0.5 * (surf - skyv)
        k = np.nonzero(prof[::-1] > mid)[0]                     # first crossing coming in from the sky
        kc = len(prof) - 1 - k[0]
        if kc + 1 >= len(prof) or kc < 1:
            continue
        f = kc + (prof[kc] - mid) / (prof[kc] - prof[kc + 1])
        rc = rr[0] + f * step
        out.append((cx + rc * math.cos(ph), cy + rc * math.sin(ph), ph))
    return np.array(out)


def refraction_arcmin(h):
    R = 1.02 / math.tan((h + 10.3 / (h + 5.11)) * D2R)
    return R * (a.pressure_hpa / 1010.0) * (283.0 / (273.0 + a.temp_c))


def radec_to_altaz(ra_deg, dec_deg, lst_deg):
    H = (lst_deg - ra_deg) * D2R; d = dec_deg * D2R; ph = a.lat * D2R
    alt = math.asin(math.sin(ph) * math.sin(d) + math.cos(ph) * math.cos(d) * math.cos(H))
    az = math.atan2(math.sin(H), math.cos(H) * math.sin(ph) - math.tan(d) * math.cos(ph))
    return alt / D2R, (az / D2R + 180) % 360


def model_curvature(t, pa_lo, pa_hi):
    """Radius of curvature (arcsec) of the refracted apparent limb over PA pa_lo..pa_hi (deg, N through E)."""
    m = me.moon(t, a.lat, a.lon)
    s = m["diam_arcmin"] / 2 / 60                           # semi-diameter, deg
    ra0, dec0, lst = m["ra_h"] * 15, m["dec"], m["lst_h"] * 15
    h0, z0 = radec_to_altaz(ra0, dec0, lst); h0a = h0 + refraction_arcmin(h0) / 60
    pts = []
    for pa in np.linspace(pa_lo, pa_hi, 60):
        dd = s * math.cos(pa * D2R); dr = s * math.sin(pa * D2R) / math.cos(dec0 * D2R)
        h, z = radec_to_altaz(ra0 + dr, dec0 + dd, lst)
        ha = h + refraction_arcmin(h) / 60
        # local tangent plane about the apparent centre (small field: flat approx is 1e-5)
        x = (z - z0) * math.cos(h0a * D2R) * 3600; y = (ha - h0a) * 3600
        pts.append((x, y))
    pts = np.array(pts)
    cx, cy, R = fit_circle(pts[:, 0], pts[:, 1])
    return R, s * 3600, m


def analyse_frame(img):
    g = ndimage.gaussian_filter(green(img), 0.7)
    p0 = sky_side_points(g)
    if len(p0) < 100:
        return None
    cx, cy, R = fit_circle(p0[:, 0], p0[:, 1])
    for it in range(2):
        rp = radial_points(g, cx, cy, R)
        if len(rp) < 50:
            return None
        x, y = rp[:, 0], rp[:, 1]
        keep = np.ones(len(x), bool)
        for _ in range(4):
            cx, cy, R = fit_circle(x[keep], y[keep], (cx, cy, R))
            res = np.hypot(x - cx, y - cy) - R
            mad = 1.4826 * np.median(np.abs(res[keep] - np.median(res[keep])))
            keep = np.abs(res) < max(3 * mad, 0.3)
    # arc midpoint direction (sensor, from the centre outwards) -> position angle on the sky
    phm = math.atan2(np.sin(rp[keep, 2]).mean(), np.cos(rp[keep, 2]).mean())
    rel = np.angle(np.exp(1j * (rp[keep, 2] - phm)))               # -pi..pi about the midpoint
    span = (rel.max() - rel.min()) / D2R
    u = np.array([math.cos(phm), math.sin(phm)])
    pa = math.degrees(math.atan2(u @ E, u @ N)) % 360
    return dict(R_px=2 * R, cx=2 * cx, cy=2 * cy, n=int(keep.sum()), n_all=len(rp), rms_px=2 * float(res[keep].std()), span_deg=float(span), pa_mid=pa)


def analyse(frames, ts):
    rows = []
    for i, (img, t) in enumerate(zip(frames, ts)):
        r = analyse_frame(img)
        if r is None:
            log("  frame %d: no usable limb" % i); continue
        tt = dt.datetime.fromtimestamp(float(t), dt.timezone.utc)
        Rm, s_arcsec, m = model_curvature(tt, r["pa_mid"] - r["span_deg"] / 2, r["pa_mid"] + r["span_deg"] / 2)
        r.update(R_model=Rm, semidiam=s_arcsec, alt=m["alt"], scale=Rm / r["R_px"])
        rows.append(r)
        log("  frame %d: R %.1f px (n %d/%d, rms %.2f px), arc %.1f deg around PA %.0f; limb curvature %.2f\" (semi-diam %.2f\", alt %.1f) -> %.5f\"/px" %
            (i, r["R_px"], r["n"], r["n_all"], r["rms_px"], r["span_deg"], r["pa_mid"], Rm, s_arcsec, m["alt"], r["scale"]))
    if not rows:
        sys.exit("no frame gave a limb")
    sc = np.array([r["scale"] for r in rows])
    F = 206264.806 * a.pixel_um * 1e-3 / sc.mean()
    log("LIMB SCALE %.5f \"/px +- %.5f (sd %.2f%%, %d frames)  ->  focal length %.0f mm (f/%.1f)  [curvature model/semi-diameter %.4f]" %
        (sc.mean(), sc.std() / math.sqrt(len(sc)), 100 * sc.std() / sc.mean(), len(sc), F, F / 203.2, np.mean([r["R_model"] / r["semidiam"] for r in rows])))
    return rows


if __name__ == "__main__":
    if a.analyse:
        z = np.load(a.analyse); analyse(z["frames"], z["ts"]); sys.exit(0)

    from daypipes import Pipes
    p = Pipes()
    frames, ts = [], []
    try:
        exp = a.exp_ms / 1000.0
        p.setup("preview", exp, a.gain, 1)
        p.grab(timeout=40)
        for it in range(4):                                   # preview bin 1 is ~10x dimmer than bin 2 at equal settings
            img, *_ = p.grab(timeout=40)
            floor, top = np.percentile(green(img), [1, 99.5])
            log("  auto %d: %.1f ms -> surface p99.5 %.0f over floor %.0f" % (it, exp * 1000, top - floor, floor))
            if 20000 <= top - floor <= 40000:
                break
            exp = float(np.clip(exp * 30000.0 / max(top - floor, 300.0), 0.0005, 0.5))
            p.setup("preview", exp, a.gain, 1); p.grab(timeout=40)
        for i in range(a.frames):
            t0 = time.time()
            img, w, h, info = p.grab(timeout=40)
            frames.append(np.asarray(img)); ts.append(t0)
            log("  frame %d %dx%d  p99.5 %.0f  (%.1f s)" % (i, w, h, np.percentile(img, 99.5), time.time() - t0))
    finally:
        try: p.close()
        except Exception: pass
    fn = os.path.join(a.outdir, "%s_%s.npz" % (time.strftime("%H%M%S"), a.tag))
    np.savez_compressed(fn, frames=np.array(frames), ts=np.array(ts))
    log("saved %s" % fn)
    rows = analyse(frames, ts)
    json.dump(rows, open(fn.replace(".npz", ".json"), "w"), indent=1)
