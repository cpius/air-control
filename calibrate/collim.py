#!/usr/bin/env python3
"""Collimation and star test on a defocused star: shadow offset, ellipticity, radial profile.

Per run: (optional) re-centre the star with planetcentre.py -> EAF to --eaf (arrives from above)
-> rtmp page, bin 1, centred square ROI -> N fresh frames, each re-registered on its own
centroid and flux-normalised (passing cloud only scales a frame) -> mean donut ->
outer edge and shadow edge found along 360 rays (50% crossings), circle-fitted ->
shadow-minus-outer offset in px, % of outer radius, and on the sky (E/N). Ring brightness
by 8 sectors (dew / coma). Annotated PNG + .npz of the mean donut.

Star test (what a misplaced barlow lens cell would show):
  * ellipticity of the outer edge (edge fit) and of the light (2nd moments), major-axis angle:
    astigmatism from a tilted element = an oval whose long axis turns ~90 deg through focus;
  * azimuthal radial profile in units of the outer radius, rim/fill ratio and outer-edge
    20-80% width: spherical aberration = inside and outside donuts that DIFFER (bright sharp
    rim one side, soft edge + brighter fill the other); a correct train gives matching profiles.
  --compare A.npz B.npz takes one inside- and one outside-focus run (equal |defocus|) and
  prints them side by side without touching the rig.
"""
import argparse, datetime as dt, math, os, subprocess, sys, time
import numpy as np, cv2
from scipy import ndimage
AC = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, os.path.join(AC, "lib"))
from daypipes import Pipes, log, move_to, focuser_pos

ap = argparse.ArgumentParser()
ap.add_argument("--eaf", type=int, default=None, help="defocused EAF position (omit = leave)")
ap.add_argument("--frames", type=int, default=30)
ap.add_argument("--exp-ms", type=float, default=20); ap.add_argument("--gain", type=int, default=200)
ap.add_argument("--roi", type=int, default=800)
ap.add_argument("--centre", action="store_true", help="re-centre the star first (planetcentre.py)")
ap.add_argument("--east", default="-0.365,-0.931", help="sky east on the sensor (x right, y down)")
ap.add_argument("--scale", type=float, default=0.3065, help="arcsec per bin-1 px")
ap.add_argument("--tag", default="collim")
ap.add_argument("--half", type=int, default=60, help="half-size of the per-frame crop box (px); never smaller")
_night = dt.datetime.now() - dt.timedelta(hours=12)       # after midnight still files under the evening's date
ap.add_argument("--out", default=os.path.expanduser("~/ASICAP/telemetry/%s/collim" % _night.strftime("%Y-%m-%d")))
ap.add_argument("--compare", nargs=2, metavar=("A", "B"), default=None,
                help="analyse two saved runs (.npz/.npy) side by side, e.g. inside vs outside focus; no rig contact")
a = ap.parse_args()
E = np.array([float(v) for v in a.east.split(",")]); E /= np.linalg.norm(E); N = np.array([-E[1], E[0]])
os.makedirs(a.out, exist_ok=True)


def circfit(x, y):
    A = np.column_stack([x, y, np.ones_like(x)]); b = x**2 + y**2
    c, *_ = np.linalg.lstsq(A, b, rcond=None)
    xc, yc = c[0] / 2, c[1] / 2; r = math.sqrt(c[2] + xc**2 + yc**2)
    res = np.hypot(x - xc, y - yc) - r
    return xc, yc, r, float(np.sqrt(np.mean(res**2)))


def robust_circ(x, y):
    xc, yc, r, rms = circfit(x, y)
    for _ in range(2):
        res = np.abs(np.hypot(x - xc, y - yc) - r)
        k = res < max(3 * rms, 1.0)
        if k.sum() < 20: break
        x, y = x[k], y[k]; xc, yc, r, rms = circfit(x, y)
    return xc, yc, r, rms, len(x)


def sky_pa(theta_down_deg):
    """Sky position angle (N through E, 0-180) of an image axis at theta from +x toward +y (y down, cv2's convention)."""
    d = np.array([math.cos(math.radians(theta_down_deg)), math.sin(math.radians(theta_down_deg))])
    return math.degrees(math.atan2(float(d @ E), float(d @ N))) % 180


def analyse(mean, eaf_now, label=""):
    """All the measurements on one mean donut; logs them and returns a dict."""
    R = mean.shape[0] // 2
    c0 = np.array([R, R], float)
    pk = np.percentile(mean, 99.5)
    ang = np.radians(np.arange(0, 360, 1.0))
    rr = np.arange(0, R, 0.25)
    outer, inner = [], []
    for t in ang:
        xs, ys = c0[0] + rr * np.cos(t), c0[1] + rr * np.sin(t)
        prof = ndimage.map_coordinates(mean, [ys, xs], order=1)
        j = int(np.argmax(prof)); top = prof[j]
        if top < 0.3 * pk: continue
        half = 0.5 * top
        o = np.nonzero(prof[j:] < half)[0]
        if len(o):
            k = j + o[0]; r_o = rr[k - 1] + (rr[k] - rr[k - 1]) * (prof[k - 1] - half) / (prof[k - 1] - prof[k] + 1e-12)
            outer.append((c0[0] + r_o * math.cos(t), c0[1] + r_o * math.sin(t)))
        i = np.nonzero(prof[:j] < half)[0]
        if len(i) and prof[0] < half:
            k = i[-1]; r_i = rr[k] + (rr[k + 1] - rr[k]) * (half - prof[k]) / (prof[k + 1] - prof[k] + 1e-12)
            inner.append((c0[0] + r_i * math.cos(t), c0[1] + r_i * math.sin(t)))
    outer, inner = np.array(outer), np.array(inner)
    xo, yo, ro, rmso, no = robust_circ(outer[:, 0], outer[:, 1])
    log("%souter edge: centre (%.2f,%.2f) r %.2f px (%.1f\"), rms %.2f, %d rays" % (label, xo, yo, ro, ro * a.scale, rmso, no))
    if len(inner) < 30:
        log("%sedge-based shadow fit: too few rays (%d)" % (label, len(inner))); xi, yi, ri = xo, yo, 0.3 * ro
    else:
        xi, yi, ri, rmsi, ni = robust_circ(inner[:, 0], inner[:, 1])
        log("%sshadow    : centre (%.2f,%.2f) r %.2f px, rms %.2f, %d rays  (obstruction %.0f%%)" % (label, xi, yi, ri, rmsi, ni, 100 * ri / ro))
    dv = np.array([xi - xo, yi - yo]); pct = 100 * np.hypot(*dv) / ro
    e_px, n_px = float(dv @ E), float(dv @ N)
    pa_img = math.degrees(math.atan2(-dv[1], dv[0]))         # image angle, CCW from +x with y up on screen
    log("%sSHADOW OFFSET: dx %+.2f dy %+.2f px = %.1f%% of radius ; on the sky %+.2f px east %+.2f px north ; image angle %.0f deg"
        % (label, dv[0], dv[1], pct, e_px, n_px, pa_img))
    # second estimate: centroid of the DARKNESS inside the ring (robust to a soft shadow edge)
    yy, xx = np.mgrid[0:mean.shape[0], 0:mean.shape[1]]
    rad = np.hypot(xx - xo, yy - yo)
    ringlev = np.percentile(mean[(rad > 0.55 * ro) & (rad < 0.85 * ro)], 50)
    dark = np.clip(ringlev - mean, 0, None) * (rad < 0.5 * ro)
    dyc, dxc = ndimage.center_of_mass(dark)
    dv2 = np.array([dxc - xo, dyc - yo]); pct2 = 100 * np.hypot(*dv2) / ro
    log("%sDARK-CENTROID OFFSET: dx %+.2f dy %+.2f px = %.1f%% of radius ; image angle %.0f deg ; shadow depth %.0f%% of ring"
        % (label, dv2[0], dv2[1], pct2, math.degrees(math.atan2(-dv2[1], dv2[0])), 100 * (1 - mean[int(round(dyc)), int(round(dxc))] / ringlev)))
    # ring brightness by sector, measured around the OUTER centre
    th = (np.degrees(np.arctan2(-(yy - yo), xx - xo)) + 360) % 360
    ring = (rad > ri + 0.25 * (ro - ri)) & (rad < ro - 0.25 * (ro - ri))
    sec = [float(mean[ring & (th >= s) & (th < s + 45)].mean()) for s in range(0, 360, 45)]
    log("%sring brightness by sector (image angle 0=right,90=up): " % label + " ".join("%d:%.2f" % (s, v / np.mean(sec)) for s, v in zip(range(0, 360, 45), sec)))
    # --- star test: ellipticity -------------------------------------------------------------
    # (1) ellipse through the outer-edge points; cv2 angle is CW-on-screen (y down) of the first axis
    (exc, eyc), (ew, eh), eang = cv2.fitEllipse(outer.astype(np.float32))
    e_ratio = min(ew, eh) / max(ew, eh); e_th = eang if ew >= eh else eang + 90
    # (2) second moments of the light inside 1.15 ro (whole donut, insensitive to edge noise)
    w = np.clip(mean, 0, None) * (rad < 1.15 * ro); W = w.sum()
    mx, my = (w * xx).sum() / W, (w * yy).sum() / W
    Mxx = (w * (xx - mx) ** 2).sum() / W; Myy = (w * (yy - my) ** 2).sum() / W; Mxy = (w * (xx - mx) * (yy - my)).sum() / W
    lam = np.linalg.eigvalsh([[Mxx, Mxy], [Mxy, Myy]]); m_ratio = math.sqrt(lam[0] / lam[1])
    m_th = math.degrees(0.5 * math.atan2(2 * Mxy, Mxx - Myy))   # y-down convention, like cv2
    log("%sELLIPTICITY: edge-fit axis ratio %.3f, major at image %.0f deg (sky PA %.0f) ; moments ratio %.3f, major at image %.0f deg (sky PA %.0f)"
        % (label, e_ratio, (-e_th) % 180, sky_pa(e_th), m_ratio, (-m_th) % 180, sky_pa(m_th)))
    # --- star test: radial profile in units of the outer radius ------------------------------
    edges = np.arange(0, 1.30001, 0.05); u = rad / ro
    prof = np.array([float(mean[(u >= lo) & (u < hi)].mean()) for lo, hi in zip(edges[:-1], edges[1:])])
    prof_n = prof / prof[(edges[:-1] >= 0.45) & (edges[:-1] < 0.95)].max()
    rim = float(mean[(u >= 0.80) & (u < 0.95)].mean()); fill = float(mean[(u >= 0.45) & (u < 0.70)].mean())
    # outer-edge 20-80% width from a fine azimuthal profile
    fe = np.arange(0.5, 1.4, 0.01); fp = np.array([float(mean[(u >= lo) & (u < lo + 0.01)].mean()) for lo in fe])
    jm = int(np.argmax(fp)); top = fp[jm]
    def cross(level):
        k = np.nonzero(fp[jm:] < level * top)[0]
        return fe[jm + k[0]] if len(k) else float("nan")
    ew2080 = (cross(0.2) - cross(0.8)) * ro
    log("%sPROFILE: rim/fill %.2f ; outer edge 20-80%% width %.1f px (%.0f%% of r, %.1f\")" % (label, rim / fill, ew2080, 100 * ew2080 / ro, ew2080 * a.scale))
    log("%s  r/ro " % label + " ".join("%.2f" % v for v in edges[:-1]))
    log("%s  I/Imax " % label + " ".join("%.2f" % v for v in prof_n))
    return dict(eaf=eaf_now, xo=xo, yo=yo, ro=ro, xi=xi, yi=yi, ri=ri, dv=dv, pct=pct, pct2=pct2,
                e_ratio=e_ratio, e_th=e_th, m_ratio=m_ratio, m_th=m_th, rim_fill=rim / fill,
                edge_w=ew2080, prof=prof_n, edges=edges)


def picture(mean, r, S=4):
    v = np.clip(mean / np.percentile(mean, 99.8), 0, 1) ** 0.7
    im = cv2.cvtColor((v * 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
    im = cv2.resize(im, None, fx=S, fy=S, interpolation=cv2.INTER_CUBIC)
    xo, yo, xi, yi, dv = r["xo"], r["yo"], r["xi"], r["yi"], r["dv"]
    cv2.circle(im, (int(xo * S), int(yo * S)), int(r["ro"] * S), (0, 200, 255), 1)
    cv2.circle(im, (int(xi * S), int(yi * S)), int(r["ri"] * S), (255, 120, 0), 1)
    cv2.drawMarker(im, (int(xo * S), int(yo * S)), (0, 200, 255), cv2.MARKER_CROSS, 16, 1)
    cv2.drawMarker(im, (int(xi * S), int(yi * S)), (255, 120, 0), cv2.MARKER_CROSS, 16, 1)
    cv2.arrowedLine(im, (int(xo * S), int(yo * S)), (int((xo + dv[0] * 10) * S), int((yo + dv[1] * 10) * S)), (0, 0, 255), 2, tipLength=0.2)
    # moment major axis, length scaled by the ellipticity (x20) so a round donut shows a stub
    L = r["ro"] * S * min(1.0, 20 * (1 - r["m_ratio"])); t = math.radians(r["m_th"])
    cx, cy = xo * S, yo * S; dx, dy = L * math.cos(t), L * math.sin(t)
    cv2.line(im, (int(cx - dx), int(cy - dy)), (int(cx + dx), int(cy + dy)), (0, 255, 0), 1)
    cv2.putText(im, "EAF %s  offset %.1f%%  axis ratio %.3f  rim/fill %.2f" % (r["eaf"], r["pct"], r["m_ratio"], r["rim_fill"]),
                (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
    return im


def load(fn):
    if fn.endswith(".npz"):
        z = np.load(fn); return z["mean"], (int(z["eaf"]) if "eaf" in z else None)
    return np.load(fn), None


if a.compare:
    res, ims = [], []
    for tag, fn in zip("AB", a.compare):
        mean, eaf = load(fn)
        log("=== %s: %s (EAF %s)" % (tag, os.path.basename(fn), eaf))
        r = analyse(mean, eaf, "%s " % tag); res.append(r); ims.append(picture(mean, r))
    A, B = res
    dth = abs(((A["m_th"] - B["m_th"]) + 90) % 180 - 90)
    log("=== COMPARE A vs B")
    log("  shadow offset %%      %6.1f  %6.1f" % (A["pct"], B["pct"]))
    log("  axis ratio (moments) %6.3f  %6.3f   major axes %.0f deg apart (astigmatism flips ~90)" % (A["m_ratio"], B["m_ratio"], dth))
    log("  rim/fill             %6.2f  %6.2f   (equal = no spherical aberration)" % (A["rim_fill"], B["rim_fill"]))
    log("  edge 20-80%% width px %6.1f  %6.1f" % (A["edge_w"], B["edge_w"]))
    log("  r/ro    " + " ".join("%5.2f" % v for v in A["edges"][:-1]))
    log("  A I/max " + " ".join("%5.2f" % v for v in A["prof"]))
    log("  B I/max " + " ".join("%5.2f" % v for v in B["prof"]))
    h = max(i.shape[0] for i in ims)
    ims = [cv2.copyMakeBorder(i, 0, h - i.shape[0], 0, 4, cv2.BORDER_CONSTANT) for i in ims]
    # profile plot underneath: A yellow, B cyan
    pw, ph = sum(i.shape[1] for i in ims), 220; plot = np.zeros((ph, pw, 3), np.uint8)
    for r, col in ((A, (0, 220, 255)), (B, (255, 220, 0))):
        x = (r["edges"][:-1] + 0.025) / 1.3 * (pw - 20) + 10; y = ph - 10 - np.clip(r["prof"], 0, 1.1) / 1.1 * (ph - 20)
        cv2.polylines(plot, [np.column_stack([x, y]).astype(np.int32)], False, col, 2)
    cv2.putText(plot, "radial profile vs r/ro: A yellow, B cyan", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
    fn = os.path.join(a.out, "%s_%s_compare.png" % (time.strftime("%H%M%S"), a.tag))
    cv2.imwrite(fn, np.vstack([np.hstack(ims), plot])); log("picture -> %s" % fn)
    sys.exit(0)

if a.centre:
    log("re-centring the star")
    subprocess.run([sys.executable, "-u", os.path.join(AC, "planetary/planetcentre.py"), "--east=%s" % a.east,
                    "--arcsec-per-px", str(a.scale), "--exp", "0.05", "--gain", "100", "--passes", "3", "--tol", "8"],
                   cwd=AC, env=os.environ, check=False)

p = Pipes()
try:
    if a.eaf is not None:
        log("EAF %s -> %d" % (focuser_pos(p), a.eaf)); move_to(p, a.eaf)
    eaf_now = focuser_pos(p)
    p.setup("rtmp", a.exp_ms / 1000.0, a.gain, 1)
    p.c("stop_exposure"); time.sleep(0.8)
    x0, y0 = (3840 - a.roi) // 2, (2160 - a.roi) // 2
    p.c("set_subframe", [{"x": x0, "y": y0, "width": a.roi, "height": a.roi}])
    p.c("set_control_value", ["Exposure", int(a.exp_ms * 1000)]); p.c("set_control_value", ["Gain", a.gain])
    p.s.air.drain_events(); p.last_sig = None; p.c("start_exposure", ["light"])
    for _ in range(2):
        p.grab(timeout=10)
    acc = None; R = None; n = 0; fluxes = []; t0 = time.time(); last = t0
    while n < a.frames and time.time() - t0 < 120:
        img, w, h, info = p.grab(timeout=10)
        if not info.get("fresh", True):
            continue
        g = ndimage.gaussian_filter(img.astype(np.float64), 1.5)
        bg = np.median(g); d = np.clip(g - bg, 0, None)
        if d.max() < 20:
            log("  frame too faint (peak %.0f) -- cloud? skipped" % d.max()); continue
        lab, k = ndimage.label(d > 0.15 * d.max())
        big = int(np.argmax(ndimage.sum(np.ones_like(d), lab, range(1, k + 1)))) + 1
        cy, cx = ndimage.center_of_mass(d * (lab == big))
        if R is None:
            area = float((lab == big).sum()); R = int(max(a.half, 1.6 * math.sqrt(area / math.pi)))
            log("  donut ~%.0f px across; cutting %d px boxes; peak %.0f/255" % (2 * math.sqrt(area / math.pi), 2 * R, img.max()))
        iy, ix = int(round(cy)), int(round(cx))
        if iy - R < 0 or ix - R < 0 or iy + R >= h or ix + R >= w:
            log("  donut too near the ROI edge (%.0f,%.0f) -- stopping; re-centre" % (cx, cy)); break
        # sub-pixel registration on the centroid
        sub = d[iy - R - 2:iy + R + 3, ix - R - 2:ix + R + 3]
        cut = ndimage.shift(sub, (iy - cy, ix - cx), order=1, mode="constant")[2:2 * R + 3, 2:2 * R + 3]
        f = cut.sum(); fluxes.append(f)
        acc = cut / f if acc is None else acc + cut / f
        n += 1
        if time.time() - last > 5:
            log("  %d/%d frames, last at ROI (%.0f,%.0f), flux %.0f" % (n, a.frames, cx, cy, f)); last = time.time()
finally:
    p.close()

if not n:
    sys.exit("no usable frames")
mean = acc / n
log("stacked %d frames (flux range %.0f..%.0f) at EAF %s" % (n, min(fluxes), max(fluxes), eaf_now))
r = analyse(mean, eaf_now)
fn = os.path.join(a.out, "%s_%s_eaf%s.png" % (time.strftime("%H%M%S"), a.tag, eaf_now))
cv2.imwrite(fn, picture(mean, r))
np.savez(fn[:-4] + ".npz", mean=mean, eaf=eaf_now, scale=a.scale, east=E)
log("picture -> %s  (+ .npz for --compare)" % fn)
