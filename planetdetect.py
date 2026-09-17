#!/usr/bin/env python3
"""Is the planet in this frame, and is the frame actually new? Rig-free, so it can
be tested on pulled clips (clipcheck.py) before it steers a mount.

WHY. On 2026-09-16/17 six 300 s Saturn clips were empty sky while satvideo logged
lines like "RECORDED 300.1s ; hold: 1169 frames measured, 9 without planet":

  * Its planet test -- peak > 10 counts over the sky and >= 30 px above half that
    peak -- passed on 5669 of the 5789 empty frames it judged during the six
    recordings (and on 71 of 72 frames sampled from the files afterwards). At
    gain 350-450 read noise and hot pixels alone reach +14..+60 counts. The
    exposure test ran the exposure up to its caps and the hold loop steered the
    mount after noise.
  * The exposure test of the first clip passed on a CACHED frame: satvideo reset
    its stale-frame hash just before start_exposure, so the first download was
    accepted whatever it was. Saturn sits at (346,485) in that test frame
    (telemetry/video/003222_test_40ms_g350.png) and at (345,483) in the last frame
    recorded 90 s earlier by the run that crashed, despite ~140 px/min of drift;
    the recording that started the same second is empty from frame 0.

DETECTION is relative to the frame's own noise, and needs the planet's size.
Measured on the pulled clips (2x2 superpixel sums, 5x5 box, sigma from the frame):

                            peak SNR      largest blob above 5 sigma
    Saturn (short/ clips)   800 - 1500    87 000 - 111 000 raw px
    empty (the six long)      4 - 8                <= 88 raw px

A detection needs ALL of:
  * a connected blob above sky + k_sigma * sigma, sigma measured on this frame;
  * its peak >= min_snr * sigma AND >= min_amp counts per raw pixel over the sky
    (the count floor decides where there is no measurable noise, e.g. the Air's
    JPEG thumbnails);
  * area >= min_area_frac of the globe's disc at the image scale: Saturn's 19"
    globe at 0.144"/px is 132 px across, so 25% is ~3 400 px. Moons, stars,
    cosmic-ray hits and hot-pixel clusters are all far smaller;
  * area <= max_area_frac of the frame, fill >= min_fill of its bounding box and
    aspect <= max_aspect: a gradient or a readout band is not a planet.

FRESHNESS. The 4800 socket serves whatever image the Air holds and imageID is a
constant, so newness can only be judged by content. FreshFrames counts a frame
only if every byte of it hashes to something never seen before in this run --
including the images the Air held BEFORE this capture started (remember()) --
and it has the ROI's geometry.
"""
import hashlib
import math
import sys
import time
from collections import namedtuple

import numpy as np
from scipy import ndimage

SATURN_DIAM_ARCSEC = 19.0     # equatorial globe near the 2026-10-04 opposition
SCALE_ARCSEC_PER_PX = 0.144   # C8 + 2x barlow + ADC, bin 1 (memory: barlow-adc-train-2026-09-16)


# -- detection ------------------------------------------------------------------

def superpixel(frame):
    """2x2 sums: each RGGB quad becomes one pixel, and the noise halves."""
    h, w = frame.shape[0] // 2 * 2, frame.shape[1] // 2 * 2
    f = frame[:h, :w].astype(np.float32)
    return f[0::2, 0::2] + f[1::2, 0::2] + f[0::2, 1::2] + f[1::2, 1::2]


def sky(s, clip=3.0, iters=4, floor=0.1):
    """Sky level and noise of a smoothed image: sigma-clipped median / std of a
    subsample. `floor` stops a noiseless image (a JPEG's flat black) reporting
    zero noise and infinite SNR; 0.1 is the quantisation noise of 8-bit data after
    the 2x2 sum and a 5x5 box."""
    v = s[1::3, 1::3].ravel().astype(np.float64)
    med = float(np.median(v))
    sig = max(1.4826 * float(np.median(np.abs(v - med))), floor)
    for _ in range(iters):
        keep = v[np.abs(v - med) <= clip * sig]
        if keep.size < 100:
            break
        med, sig = float(np.median(keep)), max(float(keep.std()), floor)
    return med, sig


class Detection:
    """What detect() found. `ok` is the verdict; the numbers are there to be logged."""

    def __init__(self, **kw):
        self.ok = False
        self.why = ""
        self.x = self.y = None          # flux-weighted centroid, raw px of the input frame
        self.peak = 0.0                 # brightest planet pixel, 8-bit counts (the exposure test's number)
        self.amp = self.snr = 0.0       # smoothed peak over the sky: 8-bit counts per raw px / sigmas
        self.area = 0                   # raw px above k_sigma
        self.fill = self.aspect = 0.0
        self.bg = self.sigma = 0.0      # sky and smoothed noise, 8-bit counts per raw px
        self.nblobs = 0
        self.min_area = 0
        self.__dict__.update(kw)

    def __str__(self):
        if self.ok:
            return "planet (%.0f,%.0f) peak %.0f/255 SNR %.0f area %d px" % (self.x, self.y, self.peak, self.snr, self.area)
        return "no planet: %s" % self.why


def min_area_px(arcsec_per_px=SCALE_ARCSEC_PER_PX, planet_diam_arcsec=SATURN_DIAM_ARCSEC, min_area_frac=0.25):
    """Smallest blob, in pixels of the image, that can be the planet."""
    return int(round(min_area_frac * math.pi / 4.0 * (planet_diam_arcsec / arcsec_per_px) ** 2))


def detect(frame, depth=8, arcsec_per_px=SCALE_ARCSEC_PER_PX, planet_diam_arcsec=SATURN_DIAM_ARCSEC,
           min_area_frac=0.25, k_sigma=5.0, min_snr=30.0, min_amp=3.0, box=5,
           max_area_frac=0.5, min_fill=0.2, max_aspect=4.0):
    """Find the planet in one frame (raw Bayer, mono, or a debayered grey image).

    arcsec_per_px is the scale of THIS image (a 400 px thumbnail of a 1000 px ROI
    is 2.5x coarser). depth=16 frames are reported in 8-bit counts."""
    unit = 4.0 * (256.0 if depth == 16 else 1.0)    # smoothed 2x2 sum -> 8-bit counts per raw px
    b = superpixel(frame)
    s = ndimage.uniform_filter(b, box, mode="nearest")
    bg, sig = sky(s)
    d = Detection(bg=bg / unit, sigma=sig / unit, min_area=min_area_px(arcsec_per_px, planet_diam_arcsec, min_area_frac))
    lab, n = ndimage.label(s > bg + k_sigma * sig)
    d.nblobs = n
    if n == 0:
        d.why = "nothing above %.0f sigma (peak SNR %.1f)" % (k_sigma, (float(s.max()) - bg) / sig)
        return d
    flux = ndimage.sum_labels(s - bg, lab, np.arange(1, n + 1))
    i = int(np.argmax(flux))
    sl = ndimage.find_objects(lab)[i]
    blob = lab[sl] == i + 1
    npx = int(blob.sum())
    hh, ww = blob.shape
    sub = np.where(blob, s[sl] - bg, 0.0)
    peak_s = float(sub.max())
    ys, xs = np.nonzero(blob)
    w = sub[ys, xs]
    d.x = 2.0 * (float((xs * w).sum() / w.sum()) + sl[1].start) + 0.5
    d.y = 2.0 * (float((ys * w).sum() / w.sum()) + sl[0].start) + 0.5
    d.area = 4 * npx
    d.fill = npx / float(hh * ww)
    d.aspect = max(hh, ww) / float(min(hh, ww))
    d.snr = peak_s / sig
    d.amp = peak_s / unit
    # Exposure-test peak: the 3rd brightest raw pixel of the core (smoothed > half its
    # peak), so one or two hot pixels on the disc cannot fake saturation.
    core = np.repeat(np.repeat(sub >= 0.5 * peak_s, 2, axis=0), 2, axis=1)
    crop = frame[2 * sl[0].start: 2 * sl[0].stop, 2 * sl[1].start: 2 * sl[1].stop]
    vals = np.asarray(crop, dtype=np.float32)[core[:crop.shape[0], :crop.shape[1]]]
    d.peak = float(np.partition(vals, -3)[-3] if vals.size >= 3 else vals.max()) / (256.0 if depth == 16 else 1.0)

    fails = []
    if d.snr < min_snr:
        fails.append("peak SNR %.1f < %.0f" % (d.snr, min_snr))
    if d.amp < min_amp:
        fails.append("peak %.1f < %.1f counts over sky" % (d.amp, min_amp))
    if d.area < d.min_area:
        fails.append("blob %d px < %d px (%.0f%% of a %.0f\" globe at %.3f\"/px)" % (
            d.area, d.min_area, 100 * min_area_frac, planet_diam_arcsec, arcsec_per_px))
    if d.area > max_area_frac * frame.size:
        fails.append("blob %d px covers >%.0f%% of the frame" % (d.area, 100 * max_area_frac))
    if d.fill < min_fill:
        fails.append("fill %.2f < %.2f" % (d.fill, min_fill))
    if d.aspect > max_aspect:
        fails.append("aspect %.1f > %.1f" % (d.aspect, max_aspect))
    d.ok = not fails
    d.why = "; ".join(fails)
    return d


def detector_args(ap):
    """The detection flags, shared by satvideo.py and clipcheck.py."""
    ap.add_argument("--arcsec-per-px", type=float, default=SCALE_ARCSEC_PER_PX, help="bin-1 image scale; sets the minimum planet area")
    ap.add_argument("--planet-diam-arcsec", type=float, default=SATURN_DIAM_ARCSEC, help="the planet's globe (Saturn ~19\" in Sept-Oct 2026)")
    ap.add_argument("--min-area-frac", type=float, default=0.25, help="a detection must cover at least this fraction of the globe's disc")
    ap.add_argument("--k-sigma", type=float, default=5.0, help="blob threshold, in sigmas of this frame's smoothed noise")
    ap.add_argument("--min-snr", type=float, default=30.0, help="blob peak over the sky, in sigmas (empty frames reach ~8, Saturn 800+)")
    ap.add_argument("--min-amp", type=float, default=3.0, help="blob peak over the sky, 8-bit counts per raw pixel (a floor for noiseless images)")


def detector_kw(a, scale=1.0):
    """detect() keyword arguments from parsed detector_args; `scale` = image px per raw px (0.4 for a 400 px thumbnail of 1000)."""
    return dict(arcsec_per_px=a.arcsec_per_px / scale, planet_diam_arcsec=a.planet_diam_arcsec,
                min_area_frac=a.min_area_frac, k_sigma=a.k_sigma, min_snr=a.min_snr, min_amp=a.min_amp)


# -- freshness -------------------------------------------------------------------

Frame = namedtuple("Frame", "img w h depth key n t")


def decode(raw, w, h, big_endian=False):
    """4800 payload -> (float32 HxW, depth). Same rules as daypipes.Pipes._download."""
    a = np.frombuffer(raw, dtype=np.uint8)
    if a.size == w * h:
        return a.reshape(h, w).astype(np.float32), 8
    if a.size == 2 * w * h:
        u = a.view(np.uint16)
        if sys.byteorder == "big" and not big_endian:
            u = u.byteswap()
        return u.reshape(h, w).astype(np.float32), 16
    raise RuntimeError("payload %d bytes for %dx%d" % (a.size, w, h))


class FreshFrames:
    """Frames that are provably new.

    download() -> (raw bytes, width, height, big_endian) of whatever the Air holds.
    A frame counts only if the hash of ALL its bytes was never seen before in this
    run and, when `want` is set, it is want=(width, height). Anything else is
    re-read (stale) or ignored (foreign: another page's or an earlier ROI's image).
    """

    def __init__(self, download, want=None, log=print, poll=0.1):
        self.download, self.want, self.log, self.poll = download, want, log, poll
        self.seen = set()
        self.n = self.stale = self.foreign = 0
        self.last_t = time.time()

    @staticmethod
    def key(raw):
        return hashlib.blake2b(raw, digest_size=16).hexdigest()

    def remember(self, tag):
        """Hash what the Air holds right now, so it can never pass as a new frame."""
        try:
            raw, w, h, _be = self.download()
        except Exception as e:
            self.log("%s: nothing to remember (%s: %s)" % (tag, e.__class__.__name__, e))
            return None
        k = self.key(raw)
        self.seen.add(k)
        self.log("%s: the Air holds a %dx%d image, hash %s -- it can never count as a new frame" % (tag, w, h, k[:8]))
        return k

    def get(self, timeout, heartbeat=5.0, what="new frame"):
        """The next fresh frame, or None if none arrives within `timeout` seconds."""
        t0 = last = time.time()
        while True:
            raw, w, h, be = self.download()
            k = self.key(raw)
            if k in self.seen:
                self.stale += 1
            else:
                self.seen.add(k)
                if self.want and (w, h) != tuple(self.want):
                    self.foreign += 1
                    self.log("  a %dx%d image is not the %dx%d ROI -> not from this capture, ignored" % (w, h, self.want[0], self.want[1]))
                else:
                    img, depth = decode(raw, w, h, be)
                    self.n += 1
                    self.last_t = time.time()
                    return Frame(img, w, h, depth, k, self.n, self.last_t)
            now = time.time()
            if now - t0 >= timeout:
                return None
            if now - last >= heartbeat:
                last = now
                self.log("  ... waiting for a %s: %.0f s, %d stale re-reads so far" % (what, now - t0, self.stale))
            time.sleep(self.poll)

    def skip(self, n=1, timeout=15.0):
        """Discard n fresh frames -- the ones that may have been exposed before a
        settings change or during a mount move. False if they did not arrive."""
        for _ in range(n):
            if self.get(timeout, what="frame to discard") is None:
                return False
        return True


def confirm(frames, find, need=2, radius=150.0, tries=6, timeout=15.0, log=print):
    """`need` CONSECUTIVE fresh frames with the planet, each within `radius` px of the
    one before. find(Frame) -> Detection. Returns (ok, detections, reason)."""
    run = []
    for _ in range(tries):
        f = frames.get(timeout, what="confirmation frame")
        if f is None:
            return False, run, "no new frame within %.0f s" % timeout
        d = find(f)
        if not d.ok:
            log("  confirm: fresh frame #%d (hash %s): %s" % (f.n, f.key[:8], d))
            run = []
            continue
        if run and math.hypot(d.x - run[-1].x, d.y - run[-1].y) > radius:
            log("  confirm: fresh frame #%d planet jumped %.0f px from the frame before -> start again" % (
                f.n, math.hypot(d.x - run[-1].x, d.y - run[-1].y)))
            run = []
        run.append(d)
        log("  confirm %d/%d: fresh frame #%d (hash %s) %s" % (len(run), need, f.n, f.key[:8], d))
        if len(run) >= need:
            return True, run, ""
    return False, run, "the planet was not on %d consecutive fresh frames in %d tries" % (need, tries)


class PlanetWatch:
    """Counts fresh frames with and without the planet; update() says when to stop."""

    def __init__(self, max_missing):
        self.max_missing = max_missing
        self.frames = self.with_planet = self.run = self.longest = 0

    def update(self, det):
        self.frames += 1
        if det.ok:
            self.with_planet += 1
            self.run = 0
            return False
        self.run += 1
        self.longest = max(self.longest, self.run)
        return self.run >= self.max_missing
