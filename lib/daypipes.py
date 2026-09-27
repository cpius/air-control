#!/usr/bin/env python3
"""Daytime focus toolkit, written 2026-09-10 while comparing the Air's three
frame pipelines on a building. Companion scripts: daysweep.py (focuser sweep
with frame pairs / auto-exposure / edge width), daycompare.py (collate sweeps),
fieldscan.py (joystick-step until structure appears), nudge.py (one pulse and
locate the band), rtmp_probe.py (why the video page fails and how to fix it).

Three ways of pulling a main-camera frame off the Air, side by side.

  preview  set_page(["preview"]); one start_exposure per frame; the image is
           taken at the Exposure `complete` event and `stop_solve` is issued
           before and after so the page's auto plate-solve never gets going.
  focus    set_page(["focus"]); one start_exposure per frame; 1:1 centre crop.
  rtmp     set_page(["rtmp"]); the page free-runs after start_exposure(["light"]);
           frames are pulled by content change.

Every grab returns (img float32 HxW, w, h, info) where info records how the
frame was obtained: wall time, whether an Exposure complete event was seen,
bit depth, and how many stale re-reads it took.
"""
import os, sys, time, hashlib, math
import numpy as np

sys.path.insert(0, "/Users/madsdorup/ASICAP/air-control")
from session import Session

HOST = os.environ.get("ASIAIR_HOST")


def host():
    """The Air's address, from ASIAIR_HOST. It moves between sessions -- run
    discover.py rather than remembering yesterday's number."""
    if not HOST:
        sys.exit("set ASIAIR_HOST to the Air's IP (it moves -- run discover.py)")
    return HOST
KEY = "/Users/madsdorup/ASICAP/air-control/embedded_key.pem"
T0 = time.time()


def log(msg):
    print("%s %+7.1fs  %s" % (time.strftime("%H:%M:%S"), time.time() - T0, msg), flush=True)


class Pipes:
    def __init__(self, host=None, key=KEY):
        self.s = Session(host or globals()["host"](), key, with_mount=False)
        self.page = None
        self.exp = None
        self.gain = None
        self.bin = None
        self.last_sig = None

    # -- setup ------------------------------------------------------------
    def c(self, m, p=None, t=25):
        return self.s.c(m, p, t=t)

    def setup(self, page, exp, gain, binning):
        """Switch page / exposure / gain / bin. stop_exposure FIRST, always."""
        s = self.s
        t = time.time()
        try:
            self.c("stop_solve")
        except Exception:
            pass
        self.c("stop_exposure")
        time.sleep(0.8)
        if page == "rtmp" and page != self.page:
            # The rtmp page fails with VideoCapture 540 "get image timeout" if a
            # stale subframe is set (measured 15:23 today). Clear it at bin 1
            # first; the page then picks its own bin-1 1920x1080 centre window.
            if self.page != "preview":
                self.c("set_page", ["preview"]); time.sleep(0.8)
            self.c("set_camera_bin", [1])
            r = self.c("set_subframe", [{"x": 0, "y": 0, "width": 3840, "height": 2160}])
            log("rtmp prep: bin 1, set_subframe full -> %s (now %s)" % (r, self.c("get_subframe")))
        if page != self.page:
            r = self.c("set_page", [page])
            log("set_page %s -> %s" % (page, r))
            time.sleep(0.8)
            if page == "rtmp":
                log("rtmp page reports bin=%s subframe=%s" % (self.c("get_camera_bin"), self.c("get_subframe")))
                binning = self.c("get_camera_bin")
        if binning != self.bin and page != "rtmp":
            r = self.c("set_camera_bin", [int(binning)])
            log("set_camera_bin %d -> %s (reads back %s)" % (binning, r, self.c("get_camera_bin")))
        if (exp, gain) != (self.exp, self.gain):
            self.c("set_control_value", ["Exposure", int(round(exp * 1_000_000))])
            self.c("set_control_value", ["Gain", int(gain)])
            log("exposure %.6fs gain %d (reads back %s / %s)" % (
                exp, gain, self.c("get_control_value", ["Exposure"]),
                self.c("get_control_value", ["Gain"])))
        self.page, self.exp, self.gain, self.bin = page, exp, gain, binning
        self.s.air.drain_events()
        # Prime the content hash with whatever the Air has cached, so the
        # first real grab cannot accept a stale frame from the previous run.
        try:
            _i, _w, _h, _d, sig, _hdr = self._download()
            self.last_sig = sig
            log("primed stale-frame hash (%dx%d cached)" % (_w, _h))
        except Exception as e:
            log("prime failed: %s" % e)
        if page == "rtmp":
            r = self.c("start_exposure", ["light"])
            log("rtmp start_exposure(light) -> %s" % r)
        log("setup done in %.1fs" % (time.time() - t))

    # -- one download -----------------------------------------------------
    def _download(self):
        hdr, files = self.s.img.get_image("get_current_img", 0)
        raw = next(iter(files.values()))
        sig = hashlib.md5(raw[::997]).hexdigest()
        w, h = hdr["width"], hdr["height"]
        a = np.frombuffer(raw, dtype=np.uint8)
        if a.size == w * h:
            img = a.reshape(h, w).astype(np.float32)
            depth = 8
        elif a.size == 2 * w * h:
            u = a.view(np.uint16)
            if sys.byteorder == "big" and not hdr["isBigEndian"]:
                u = u.byteswap()
            img = u.reshape(h, w).astype(np.float32)
            depth = 16
        else:
            raise RuntimeError("payload %d bytes for %dx%d" % (a.size, w, h))
        return img, w, h, depth, sig, hdr

    def _events(self, want=("complete", "downloading"), timeout=30.0):
        """Wait for an Exposure event in `want`; returns (seen, all_states)."""
        t0 = time.time()
        states = []
        while time.time() - t0 < timeout:
            for e in self.s.air.drain_events():
                if e.get("Event") == "Exposure":
                    states.append(e.get("state"))
                    if e.get("state") in want:
                        return True, states
                    if e.get("state") in ("idle", "cancel", "fail") and states[-1] != "start":
                        pass
            time.sleep(0.05)
        return False, states

    # -- frames -----------------------------------------------------------
    def grab(self, timeout=40.0):
        """One NEW frame from whichever page is set up."""
        t = time.time()
        info = {"page": self.page, "exp": self.exp, "gain": self.gain, "bin": self.bin}
        if self.page == "preview":
            try:
                self.c("stop_solve")
            except Exception:
                pass
            self.s.air.drain_events()
            r = self.c("start_exposure")
            seen, states = self._events(timeout=timeout)
            info["event"] = seen
            info["states"] = states
            if not seen:
                log("preview: no Exposure complete event in %.0fs (states %s)" % (timeout, states))
            img, w, h, depth, sig, hdr = self._download()
            stale = 0
            while sig == self.last_sig and stale < 30:
                stale += 1
                time.sleep(0.2)
                img, w, h, depth, sig, hdr = self._download()
            try:
                self.c("stop_solve")
            except Exception:
                pass
        elif self.page == "focus":
            self.s.air.drain_events()
            r = self.c("start_exposure")
            # The focus page emits no Exposure complete event (measured
            # 15:11 today): poll the image socket by content instead.
            time.sleep(max(self.exp, 0.05))
            img, w, h, depth, sig, hdr = self._download()
            stale = 0
            while sig == self.last_sig and stale < int(timeout / 0.15):
                stale += 1
                time.sleep(0.15)
                img, w, h, depth, sig, hdr = self._download()
            states = [e.get("state") for e in self.s.air.drain_events() if e.get("Event") == "Exposure"]
            info["event"] = ("complete" in states) or ("downloading" in states)
            info["states"] = states
        elif self.page == "rtmp":
            img, w, h, depth, sig, hdr = self._download()
            stale = 0
            timeout = min(timeout, 15.0)
            while sig == self.last_sig and stale < int(timeout / 0.1):
                stale += 1
                time.sleep(0.1)
                img, w, h, depth, sig, hdr = self._download()
            ev = [e.get("state") for e in self.s.air.drain_events() if e.get("Event") == "Exposure"]
            info["states"] = ev
            info["event"] = None
        else:
            raise RuntimeError("no page set up")
        fresh = sig != self.last_sig
        self.last_sig = sig
        info.update(w=w, h=h, depth=depth, stale_reads=stale, fresh=fresh,
                    dt=time.time() - t, hdr=hdr)
        return img, w, h, info

    def close(self):
        try:
            self.c("stop_solve")
        except Exception:
            pass
        try:
            self.c("stop_exposure")
        except Exception:
            pass
        self.s.close()


# -- focuser ----------------------------------------------------------------
# Allowed EAF range. 2026-09-10 session bound was +/-10000 around 44956;
# override with EAF_MIN / EAF_MAX or daysweep.py --limits LO,HI.
LO = int(os.environ.get("EAF_MIN", 34956))
HI = int(os.environ.get("EAF_MAX", 54956))


def focuser_pos(p):
    return int(p.c("get_focuser_position"))


def move_to(p, pos, timeout=120):
    pos = int(pos)
    lo, hi = LO, HI
    if not (lo <= pos <= hi):
        raise ValueError("position %d outside the permitted %d..%d" % (pos, lo, hi))
    start = focuser_pos(p)
    n = abs(pos - start)
    if n == 0:
        return start
    log("  EAF %d -> %d (%+d steps)" % (start, pos, pos - start))
    p.c("move_focuser", [pos])
    t0 = time.time()
    last = t0
    while time.time() - t0 < timeout:
        time.sleep(0.25)
        st = p.c("get_focuser_state")
        if isinstance(st, dict) and st.get("state") == "idle":
            got = focuser_pos(p)
            if got != pos:
                log("  EAF settled at %d, wanted %d" % (got, pos))
            return got
        if time.time() - last > 5:
            last = time.time()
            log("  ... EAF moving, at %s (%.0fs)" % (p.c("get_focuser_position"), time.time() - t0))
    raise TimeoutError("EAF did not settle")


# -- metrics ----------------------------------------------------------------
def superpix(img):
    """Fold 2x2 Bayer quads into one superpixel (mean). Works whether or not
    the frame is actually a mosaic; costs a factor 2 in scale."""
    h, w = img.shape
    h2, w2 = (h // 2) * 2, (w // 2) * 2
    return img[:h2, :w2].reshape(h2 // 2, 2, w2 // 2, 2).mean(axis=(1, 3))


def bayer_period(img):
    """Lag-1 vs lag-2 row autocorrelation of the raw frame: a mosaic shows
    r2 >> r1."""
    c = img[::3, :].astype(np.float64)
    c = c - c.mean(axis=1, keepdims=True)
    v = (c * c).mean()
    if v <= 0:
        return 0.0, 0.0
    r1 = (c[:, :-1] * c[:, 1:]).mean() / v
    r2 = (c[:, :-2] * c[:, 2:]).mean() / v
    return float(r1), float(r2)


def boxmean(img, k):
    from numpy.lib.stride_tricks import sliding_window_view
    p = np.pad(img, k // 2, mode="edge")
    return sliding_window_view(p, (k, k)).mean(axis=(2, 3))


def metrics(img, roi=None):
    """Sharpness numbers for an extended scene.

    tenengrad  mean squared Sobel gradient / mean^2   (contrast-normalised)
    lapvar     variance of the Laplacian / mean^2
    texture    band-pass (3 vs 15 px box) std / mean * 1000
    All computed on the 2x2-folded frame, in the central ROI.
    """
    sp = superpix(img)
    h, w = sp.shape
    if roi is None:
        c = sp[h // 4: 3 * h // 4, w // 4: 3 * w // 4]
    else:
        x0, y0, x1, y1 = roi
        c = sp[y0:y1, x0:x1]
    c = c.astype(np.float64)
    mean = max(float(c.mean()), 1.0)
    # 3x3 median kills surviving hot pixels
    from numpy.lib.stride_tricks import sliding_window_view
    cm = np.median(sliding_window_view(np.pad(c, 1, mode="edge"), (3, 3)), axis=(2, 3))
    gx = cm[:, 2:] - cm[:, :-2]
    gy = cm[2:, :] - cm[:-2, :]
    ten = float((gx[1:-1, :] ** 2 + gy[:, 1:-1] ** 2).mean()) / mean ** 2 * 1e4
    lap = (cm[1:-1, 1:-1] * 4 - cm[:-2, 1:-1] - cm[2:, 1:-1] - cm[1:-1, :-2] - cm[1:-1, 2:])
    lapvar = float(lap.var()) / mean ** 2 * 1e4
    bp = boxmean(cm, 3) - boxmean(cm, 15)
    tex = float(bp.std()) / mean * 1000
    # illumination-invariant: fine-scale power relative to mid-scale power
    b3, b9, b41 = boxmean(cm, 3), boxmean(cm, 9), boxmean(cm, 41)
    fine = float((b3 - b9).std()); mid = float((b9 - b41).std())
    ratio = fine / max(mid, 1e-6)
    return dict(mean=mean, tenengrad=ten, lapvar=lapvar, texture=tex, ratio=ratio,
                fine=fine, mid=mid,
                p1=float(np.percentile(c, 1)), p99=float(np.percentile(c, 99)),
                max=float(c.max()))


def save_png(img, path, shrink=1):
    """Autostretched 8-bit PNG (cv2). Creates the directory if needed."""
    import cv2
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    a = img[::shrink, ::shrink]
    lo, hi = np.percentile(a, 0.5), np.percentile(a, 99.7)
    hi = max(hi, lo + 1)
    g = np.clip((a - lo) / (hi - lo), 0, 1) ** 0.6 * 255
    cv2.imwrite(path, g.astype(np.uint8))
    return path


# -- pair metrics: noise-corrected, illumination-normalised -------------------
def _prep(img, roi=None):
    from numpy.lib.stride_tricks import sliding_window_view
    sp = superpix(img)
    h, w = sp.shape
    if roi is None:
        c = sp[h // 4: 3 * h // 4, w // 4: 3 * w // 4]
    else:
        x0, y0, x1, y1 = roi
        c = sp[y0:y1, x0:x1]
    c = c.astype(np.float64)
    return np.median(sliding_window_view(np.pad(c, 1, mode="edge"), (3, 3)), axis=(2, 3))


def _powers(cm):
    b3, b9, b41 = boxmean(cm, 3), boxmean(cm, 9), boxmean(cm, 41)
    gx = cm[:, 2:] - cm[:, :-2]
    gy = cm[2:, :] - cm[:-2, :]
    ten = float((gx[1:-1, :] ** 2 + gy[:, 1:-1] ** 2).mean())
    return float((b3 - b9).var()), float((b9 - b41).var()), ten


def metrics_pair(img1, img2, roi=None):
    """Sharpness from two frames at one focuser position.

    The difference frame carries only noise, so its fine-scale and gradient
    power are subtracted from the signal frames'. Fine power is then divided
    by mid-scale power, which follows the scene contrast under sun or cloud.
    """
    c1, c2 = _prep(img1, roi), _prep(img2, roi)
    mean = max(float((c1.mean() + c2.mean()) / 2), 1.0)
    f1, m1, t1 = _powers(c1)
    f2, m2, t2 = _powers(c2)
    d = (c1 - c2) / np.sqrt(2.0)
    fn, mn, tn = _powers(d)
    fine = max((f1 + f2) / 2 - fn, 0.0)
    mid = max((m1 + m2) / 2 - mn, 1e-9)
    ten = max((t1 + t2) / 2 - tn, 0.0)
    return dict(mean=mean, fine=np.sqrt(fine), mid=np.sqrt(mid), noise=np.sqrt(fn),
                ratio=np.sqrt(fine / mid), ten_corr=ten / mean ** 2 * 1e4,
                fine_norm=np.sqrt(fine) / mean * 1000,
                p99=float(np.percentile(c1, 99)), max=float(c1.max()))


def edge_width(img, bands=((200, 280), (300, 380), (400, 480), (500, 580), (600, 680)),
               rows=(10, None)):
    """Effective width (superpixels) of the strongest horizontal edge in the
    upper part of the frame: contrast / peak gradient of the column-averaged
    profile, per column band, median over bands. Linear in defocus, blind to
    illumination."""
    sp = superpix(img).astype(np.float64)
    h, w = sp.shape
    r0, r1 = rows[0], rows[1] or h // 2
    out = []
    for x0, x1 in bands:
        prof = sp[:, x0:x1].mean(axis=1)
        prof = np.convolve(prof, np.ones(3) / 3, mode="same")
        g = np.gradient(prof)
        seg = np.abs(g[r0:r1])
        i = int(np.argmax(seg)) + r0
        lo, hi = max(0, i - 60), min(h, i + 60)
        amp = np.percentile(prof[lo:hi], 95) - np.percentile(prof[lo:hi], 5)
        if amp > 50 and seg.max() > 0:
            out.append((amp / seg.max(), i, amp))
    if not out:
        return None, None, None
    ws = sorted(out, key=lambda t: t[0])
    m = ws[len(ws) // 2]
    return float(m[0]), int(m[1]), float(m[2])


def star_metrics(img, nsig=8.0):
    """Brightest star in the frame (raw px, no Bayer fold so a focused star is
    not diluted): background-subtracted HFD, peak, flux, equivalent diameter
    at half maximum, centroid. Returns None if nothing clears nsig."""
    from scipy import ndimage
    a = img.astype(np.float64)
    bg = float(np.median(a)); sig = max(1.0, 1.4826 * float(np.median(np.abs(a[::4, ::4] - bg))))
    sm = ndimage.uniform_filter(a, 5)
    lab, n = ndimage.label(sm > bg + nsig * sig)
    if n == 0:
        return None
    sizes = ndimage.sum(a - bg, lab, index=range(1, n + 1))
    i = int(np.argmax(sizes)) + 1
    ys, xs = np.nonzero(lab == i)
    if xs.size < 6:
        return None
    y0, y1, x0, x1 = max(ys.min() - 40, 0), min(ys.max() + 41, a.shape[0]), max(xs.min() - 40, 0), min(xs.max() + 41, a.shape[1])
    box = a[y0:y1, x0:x1] - bg
    peak = float(box.max())
    w = np.clip(box, 0, None)
    Y, X = np.mgrid[y0:y1, x0:x1]
    flux = float(w.sum())
    cy = float((w * Y).sum() / flux); cx = float((w * X).sum() / flux)
    r = np.hypot(Y - cy, X - cx).ravel(); ww = w.ravel()
    order = np.argsort(r); cum = np.cumsum(ww[order])
    hfr = float(r[order][np.searchsorted(cum, cum[-1] / 2)])
    half = int((box > peak / 2).sum())
    return dict(x=cx, y=cy, peak=peak + bg, flux=flux, hfd=2 * hfr, diam=2 * math.sqrt(half / math.pi), bg=bg, sig=sig)
