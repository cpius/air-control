#!/usr/bin/env python3
"""Evening bootstrap: EAF to the sky prior, Go Home, tracking on, goto a bright
star, then a preview frame with a blob report so we can see what landed."""
import argparse, math, os, sys, time, json
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daypipes import host, Pipes, log, save_png, superpix, move_to, focuser_pos
from mount import Mount

def jnow(ra_h_2000, dec_2000, year=2026.70):
    """Rigorous-enough precession J2000 -> JNow (arcsec-level for 26 yr)."""
    T = (year - 2000.0) / 100.0
    zeta = (2306.2181 * T + 0.30188 * T**2) / 3600.0
    z = (2306.2181 * T + 1.09468 * T**2) / 3600.0
    theta = (2004.3109 * T - 0.42665 * T**2) / 3600.0
    a = math.radians(ra_h_2000 * 15); d = math.radians(dec_2000)
    zr, zzr, thr = map(math.radians, (zeta, z, theta))
    A = math.cos(d) * math.sin(a + zr)
    B = math.cos(thr) * math.cos(d) * math.cos(a + zr) - math.sin(thr) * math.sin(d)
    C = math.sin(thr) * math.cos(d) * math.cos(a + zr) + math.cos(thr) * math.sin(d)
    ra = (math.degrees(math.atan2(A, B)) + math.degrees(zzr)) % 360
    dec = math.degrees(math.asin(C))
    return ra / 15.0, dec

STARS = {"vega": (18.615649, 38.783689), "arcturus": (14.261030, 19.182410), "deneb": (20.690532, 45.280339), "altair": (19.846389, 8.868322)}

def blobs(img, nsig=8.0, top=8):
    sp = superpix(img)
    bg = float(np.median(sp)); sig = max(1.0, 1.4826 * float(np.median(np.abs(sp[::4, ::4] - bg))))
    sm = ndimage.uniform_filter(sp, 3)
    lab, n = ndimage.label(sm > bg + nsig * sig)
    out = []
    for i in range(1, n + 1):
        m = lab == i
        area = int(m.sum())
        if area < 4:
            continue
        ys, xs = np.nonzero(m)
        w = sp[m] - bg
        flux = float(w.sum()); peak = float(sp[m].max())
        cy = float((w * ys).sum() / flux) if flux > 0 else ys.mean(); cx = float((w * xs).sum() / flux) if flux > 0 else xs.mean()
        # half-flux diameter within the blob region
        r = np.hypot(ys - cy, xs - cx); order = np.argsort(r); cum = np.cumsum(w[order])
        hfr = float(r[order][np.searchsorted(cum, cum[-1] / 2)]) if cum[-1] > 0 else 0
        out.append(dict(x=cx * 2, y=cy * 2, area_spx=area, flux=flux, peak=peak, hfd_bin2=2 * hfr * 2, diam_bin2=2 * 2 * math.sqrt(area / math.pi)))
    out.sort(key=lambda b: -b["flux"])
    return bg, sig, out[:top]

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--star", default="vega")
    ap.add_argument("--eaf", type=int, default=44956)
    ap.add_argument("--home", action="store_true")
    ap.add_argument("--goto", action="store_true")
    ap.add_argument("--exp", type=float, default=1.0)
    ap.add_argument("--gain", type=int, default=100)
    ap.add_argument("--frames", type=int, default=1)
    ap.add_argument("--tag", default="find")
    a = ap.parse_args()
    ra0, de0 = STARS[a.star]; ra, de = jnow(ra0, de0)
    m = Mount(host())
    st = m.state()
    lst = st["sidereal_time"]; ha = ((lst - ra + 12) % 24) - 12
    lat = math.radians(st["Lat"]); dr = math.radians(de); hr = math.radians(ha * 15)
    alt = math.degrees(math.asin(math.sin(lat) * math.sin(dr) + math.cos(lat) * math.cos(dr) * math.cos(hr)))
    log("%s JNow RA %.4fh Dec %+.4f ; LST %.4f -> HA %+.2fh, alt %.1f" % (a.star, ra, de, lst, ha, alt))
    log("mount now: RA %.4f Dec %.4f Alt %.2f Az %.2f track=%s homed=%s pier=%s" % (st["RA"], st["Dec"], st["Alt"], st["Az"], st["is_enable_track"], st["is_home_succeed"], st["pier_side"]))
    p = Pipes()
    try:
        log("EAF %s -> %d" % (focuser_pos(p), a.eaf)); move_to(p, a.eaf)
        if a.home:
            log("Go Home (scope_park) ...")
            t0 = time.time(); m.park(wait=True, timeout=150, on_progress=lambda ev: log("   %s" % ev))
            st = m.state(); log("homed in %.0fs: RA %.4f Dec %.4f Alt %.2f Az %.2f homed=%s" % (time.time() - t0, st["RA"], st["Dec"], st["Alt"], st["Az"], st["is_home_succeed"]))
        log("tracking on -> %s ; now %s" % (m.set_tracking(True), m.tracking()))
        if a.goto:
            s0 = m.state(); t0 = time.time()
            try:
                m.goto(ra, de, wait=True, timeout=150, on_progress=lambda ev: log("   %s" % ev))
            except Exception as ex:
                log("goto raised: %s" % ex)
            st = m.state()
            log("after goto (%.0fs): RA %.4f Dec %.4f Alt %.2f Az %.2f ; moved %.2f deg in Dec, %.2f h in RA" % (
                time.time() - t0, st["RA"], st["Dec"], st["Alt"], st["Az"], st["Dec"] - s0["Dec"], st["RA"] - s0["RA"]))
        p.setup("preview", a.exp, a.gain, 2)
        for i in range(a.frames):
            img, w, h, info = p.grab()
            bg, sig, bl = blobs(img)
            fn = "frames/%s_%d.png" % (a.tag, i)
            save_png(img, fn, shrink=2)
            log("frame %d: %dx%d bg %.0f sigma %.1f max %.0f  -> %s" % (i, w, h, bg, sig, img.max(), fn))
            for b in bl:
                log("   blob x=%5.0f y=%5.0f  flux %9.0f  peak %6.0f  area %4d spx  diam %5.1f px  hfd %5.1f px (bin2)" % (
                    b["x"], b["y"], b["flux"], b["peak"], b["area_spx"], b["diam_bin2"], b["hfd_bin2"]))
        st = m.state(); log("mount: RA %.4f Dec %.4f Alt %.2f Az %.2f track=%s" % (st["RA"], st["Dec"], st["Alt"], st["Az"], st["is_enable_track"]))
    finally:
        p.close(); m.close(); log("closed")
