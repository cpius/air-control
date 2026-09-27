#!/usr/bin/env python3
"""Measure Saturn's moons live in video frames and decide when a moon video has enough signal.

Used by satmoonvideo.py (live) and runnable on saved frames (--replay) to test it.

  * Horizons (one query per body, a window around the start) gives each moon's offset from Saturn;
    --east / --scale put it on the sensor. Saturn itself is measured every frame (saturated blob).
  * Every frame (luminance at 2x2-binned scale): each moon is searched near its prediction, its
    per-frame SNR measured, and a cut-out added to that moon's running stack -- centred on the moon's
    own centroid when it is bright enough to be seen in one frame (its seeing jitter is not Saturn's),
    else on the prediction plus the offset learned so far.
  * projected SNR = SNR of the running stack x sqrt(recorded frames / sampled frames) x sqrt(--keep):
    the AVI holds more frames than the preview stream shows us, and the stacker keeps only the best.
  * plan_roi(): the readout window (bin-1 sensor px) that holds Saturn and the wanted moons, and
    where Saturn must sit in it.

    python3 -u planetary/moonmeter.py --replay '../telemetry/2026-09-27/satmoons/010504_*_bin2.npy' --east=0.9875,0.1578
"""
import argparse, datetime as dt, glob, math, os, re, sys, time, urllib.parse, urllib.request
import numpy as np
from scipy import ndimage

CODES = {"Mimas": "601", "Enceladus": "602", "Tethys": "603", "Dione": "604", "Rhea": "605", "Titan": "606"}
SENSOR = (3840, 2160)


def horizons(cmd, t0, t1, step="1 m"):
    ut = lambda ts: dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    q = dict(format="text", COMMAND="'%s'" % cmd, EPHEM_TYPE="'OBSERVER'", CENTER="'coord@399'", COORD_TYPE="'GEODETIC'",
             SITE_COORD="'12.5553,55.6894,0.02'", START_TIME="'%s'" % ut(t0), STOP_TIME="'%s'" % ut(t1), STEP_SIZE="'%s'" % step,
             QUANTITIES="'1'", ANG_FORMAT="'DEG'", CSV_FORMAT="'YES'")
    txt = urllib.request.urlopen("https://ssd.jpl.nasa.gov/api/horizons.api?" + urllib.parse.urlencode(q), timeout=60).read().decode()
    rows = []
    for line in txt.split("$$SOE")[1].split("$$EOE")[0].strip().splitlines():
        f = line.split(",")
        tt = dt.datetime.strptime(f[0].strip(), "%Y-%b-%d %H:%M").replace(tzinfo=dt.timezone.utc).timestamp()
        nums = [float(v) for v in f[1:] if re.fullmatch(r"\s*-?\d+\.\d+\s*", v)]
        rows.append((tt, nums[0], nums[1]))
    return np.array(rows)


class MoonMeter:
    def __init__(self, t_start, east, scale_bin2, moons, required, target_snr=40.0, keep=0.25,
                 window=(-600, 1800), log=print, box=16, search=12):
        E = np.array(east, float); self.E = E / np.linalg.norm(E); self.N = np.array([-self.E[1], self.E[0]])
        self.scale = scale_bin2; self.moons = list(moons); self.required = [m for m in required if m in self.moons]
        self.target = target_snr; self.keep = keep; self.log = log; self.box = box; self.search = search
        t0, t1 = t_start + window[0], t_start + window[1]
        self.eph = {"Saturn": horizons("699", t0, t1)}
        for m in self.moons:
            self.eph[m] = horizons(CODES[m], t0, t1)
        self.learned = {m: [] for m in self.moons}                 # measured - predicted, px (bin-2 luminance frame)
        self.stack = {m: np.zeros((2 * box + 1, 2 * box + 1)) for m in self.moons}
        self.nstack = {m: 0 for m in self.moons}
        self.last = {m: None for m in self.moons}
        self.snr2 = {m: 0.0 for m in self.moons}                    # sum of per-frame SNR^2 over frames where the moon is plainly seen
        self.frames = 0; self.saturn = None
        self.skip = set()

    def offset_arcsec(self, m, t):
        tb, sb = self.eph[m], self.eph["Saturn"]
        ra, de = np.interp(t, tb[:, 0], tb[:, 1]), np.interp(t, tb[:, 0], tb[:, 2])
        rs, ds = np.interp(t, sb[:, 0], sb[:, 1]), np.interp(t, sb[:, 0], sb[:, 2])
        return np.array([(ra - rs) * 3600 * math.cos(math.radians(ds)), (de - ds) * 3600])

    def offset_px(self, m, t, scale=None):                         # sensor px (at `scale`, default bin-2) of moon m from Saturn
        e, n = self.offset_arcsec(m, t)
        return (e * self.E + n * self.N) / (scale or self.scale)

    def plan_roi(self, t, margin_arcsec=15.0, min_h=400, bin1_scale=None, sensor=SENSOR, want=None):
        """Readout window (x, y, w, h, bin-1 px) holding Saturn and the moons in `want`, and Saturn's spot in it.
        Moons that cannot fit on the sensor are dropped, farthest first."""
        s1 = bin1_scale or self.scale / 2
        want = list(want or self.moons)
        while True:
            pts = [np.zeros(2)] + [self.offset_px(m, t, s1) for m in want]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]; mg = margin_arcsec / s1
            # the floor goes on BOTH sides: on 2026-09-27 the camera had E-W along sensor y, so the moons spread
            # vertically and an unfloored width left a ~224 px window across the hold's 120 px deadband
            w = int(math.ceil(max(max(xs) - min(xs) + 2 * mg, min_h) / 32) * 32); h = int(math.ceil(max(max(ys) - min(ys) + 2 * mg, min_h) / 32) * 32)
            if w <= sensor[0] and h <= sensor[1] or not want:
                break
            far = max(want, key=lambda m: np.hypot(*self.offset_px(m, t, s1)))
            self.log("plan: %s does not fit on the sensor (window %dx%d) -- left out" % (far, w, h)); want.remove(far)
        w, h = min(w, sensor[0]), min(h, sensor[1])
        sx, sy = -min(xs) + mg, -min(ys) + mg                        # Saturn in the window
        sx += (w - (max(xs) - min(xs) + 2 * mg)) / 2; sy += (h - (max(ys) - min(ys) + 2 * mg)) / 2
        x0 = int(round((sensor[0] / 2 - sx) / 2) * 2); y0 = int(round((sensor[1] / 2 - sy) / 2) * 2)   # Saturn at the sensor centre -> sx, sy
        x0c, y0c = min(max(x0, 0), sensor[0] - w), min(max(y0, 0), sensor[1] - h)
        self.skip = set(self.moons) - set(want)
        # Saturn's target is ALWAYS its layout spot (sx, sy); when the window had to be clamped to the sensor
        # edge, Saturn at the sensor centre is not there yet and the hold moves it (x0 - x0c px)
        return dict(x=x0c, y=y0c, width=w, height=h), (sx, sy), want

    @staticmethod
    def find_saturn(lum):
        sm = ndimage.gaussian_filter(lum, 2); med = float(np.median(lum)); pk = sm.max()
        if pk - med < 20: return None
        lab, n = ndimage.label(sm > med + 0.5 * (pk - med)); ar = ndimage.sum(np.ones_like(sm), lab, range(1, n + 1))
        k = int(np.argmax(ar)) + 1
        if ar[k - 1] < 200: return None
        cy, cx = ndimage.center_of_mass(np.ones_like(sm), lab, k); return np.array([cx, cy])

    def measure(self, lum, pos):
        """Background-subtracted smoothed peak near pos, its SNR and centroid (bin-2 luminance px)."""
        r = self.search + 10; x, y = int(round(pos[0])), int(round(pos[1]))
        if x - r < 0 or y - r < 0 or x + r >= lum.shape[1] or y + r >= lum.shape[0]: return None
        sub = lum[y - r:y + r + 1, x - r:x + r + 1].astype(np.float64)
        bg = ndimage.median_filter(sub, 9); z = ndimage.gaussian_filter(sub - bg, 1.0)
        yy, xx = np.mgrid[-r:r + 1, -r:r + 1]; ring = (np.hypot(xx, yy) > self.search + 2)
        sd = 1.4826 * np.median(np.abs(z[ring] - np.median(z[ring]))) + 1e-9
        inner = np.hypot(xx, yy) <= self.search; zz = np.where(inner, z, -np.inf)
        j = np.unravel_index(int(np.argmax(zz)), z.shape); pk = z[j]
        w = np.clip(z, 0, None) * (np.hypot(xx - (j[1] - r), yy - (j[0] - r)) < 3)
        if w.sum() <= 0: return None
        cy = (w * yy).sum() / w.sum(); cx = (w * xx).sum() / w.sum()
        return dict(snr=float(pk / sd), x=x + cx, y=y + cy)

    def update(self, lum, t):
        """One frame: luminance at bin-2 scale, its time. Returns Saturn's position or None."""
        S = self.find_saturn(lum)
        if S is None: return None
        self.saturn = S; self.frames += 1
        for m in self.moons:
            if m in self.skip: continue
            lo = np.median(self.learned[m], axis=0) if len(self.learned[m]) >= 3 else np.zeros(2)
            pred = S + self.offset_px(m, t) + lo
            got = self.measure(lum, pred)
            if got is None: self.last[m] = None; continue
            self.last[m] = got
            if got["snr"] >= 6:                                     # seen in one frame: learn its offset, stack on itself
                self.snr2[m] += got["snr"] ** 2
                self.learned[m].append(np.array([got["x"], got["y"]]) - (S + self.offset_px(m, t)))
                self.learned[m] = self.learned[m][-30:]
                c = np.array([got["x"], got["y"]])
            else:
                c = pred
            b = self.box; x, y = int(round(c[0])), int(round(c[1]))
            if b <= x < lum.shape[1] - b and b <= y < lum.shape[0] - b:
                sub = lum[y - b - 6:y + b + 7, x - b - 6:x + b + 7].astype(np.float64)
                if sub.shape == (2 * b + 13, 2 * b + 13):
                    sub = sub - ndimage.median_filter(sub, 9)
                    sub = ndimage.shift(sub, (y - c[1], x - c[0]), order=1, mode="nearest")[6:-6, 6:-6]
                    self.stack[m] += sub; self.nstack[m] += 1
        return S

    def stacked_snr(self, m):
        n = self.nstack[m]
        if n == 0: return 0.0
        z = ndimage.gaussian_filter(self.stack[m] / n, 1.0); b = self.box
        yy, xx = np.mgrid[-b:b + 1, -b:b + 1]; ring = np.hypot(xx, yy) > b - 4     # beyond ~2.5": clear of a 0.8" moon's wings
        sd = 1.4826 * np.median(np.abs(z[ring] - np.median(z[ring]))) + 1e-12
        return float((z[b - 1:b + 2, b - 1:b + 2].max() - np.median(z[ring])) / sd)   # at the centre only: no max-of-noise bias

    def status(self, recorded_frames=None):
        """{moon: (projected SNR, done)} and whether every required moon is done."""
        ratio = max(1.0, (recorded_frames or self.frames) / max(self.frames, 1))
        out = {}
        for m in self.moons:
            if m in self.skip: continue
            # bright moons: per-frame SNRs add in quadrature (robust to their wide wings); faint ones: the stack itself
            proj = max(math.sqrt(self.snr2[m]), self.stacked_snr(m)) * math.sqrt(ratio) * math.sqrt(self.keep)
            out[m] = (proj, proj >= self.target)
        done = all(out.get(m, (0, False))[1] for m in self.required if m not in self.skip)
        return out, done

    def line(self, recorded_frames=None):
        st, done = self.status(recorded_frames)
        return " | ".join("%s %s%.0f%s" % (m, "*" if m in self.required else "", s, " ok" if ok else "") for m, (s, ok) in st.items()), done


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", required=True, help="glob of saved bin-2 luminance frames (satmoons .npy); file time = capture time")
    ap.add_argument("--east", default="0.9875,0.1578"); ap.add_argument("--scale", type=float, default=0.19936, help='"/px of the frames')
    ap.add_argument("--moons", default="Titan,Rhea,Dione,Tethys,Enceladus,Mimas"); ap.add_argument("--required", default="Titan,Rhea,Dione,Tethys")
    ap.add_argument("--target-snr", type=float, default=40.0); ap.add_argument("--keep", type=float, default=0.25)
    ap.add_argument("--recorded-per-sampled", type=float, default=1.0, help="replay: pretend the AVI holds this many frames per sampled one")
    a = ap.parse_args()
    files = sorted(glob.glob(a.replay), key=os.path.getmtime)
    mm = MoonMeter(os.path.getmtime(files[0]), [float(v) for v in a.east.split(",")], a.scale, a.moons.split(","), a.required.split(","),
                   a.target_snr, a.keep)
    win, spot, want = mm.plan_roi(os.path.getmtime(files[0]))
    print("plan: window %s, Saturn at (%.0f,%.0f) in it, moons %s" % (win, spot[0], spot[1], want))
    for i, f in enumerate(files, 1):
        lum = np.load(f).astype(np.float64); t = os.path.getmtime(f)
        S = mm.update(lum, t)
        per = " ".join("%s:%.0f" % (m[:3], v["snr"]) for m, v in mm.last.items() if v)
        txt, done = mm.line(int(mm.frames * a.recorded_per_sampled))
        print("frame %2d  Saturn %s  per-frame SNR %s\n          projected: %s%s" % (i, None if S is None else "(%.0f,%.0f)" % tuple(S), per, txt, "  -> DONE" if done else ""))
    for m in mm.moons:
        if mm.learned[m]:
            lo = np.median(mm.learned[m], axis=0); print("  %-9s learned offset (%+.1f,%+.1f) px = %.2f\"" % (m, lo[0], lo[1], np.hypot(*lo) * a.scale))
