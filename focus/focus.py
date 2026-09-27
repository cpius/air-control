#!/usr/bin/env python3
"""Fast autofocus for the ASIAIR — V-curve on `move_focuser`, no `start_auto_focuse`.

The Air's own `start_auto_focuse` is a trap: with no params it does not sweep,
it replays a remembered result and leaves the camera faulted (RPC_METHODS.md).
This sweeps client-side in 35-65 s, against 40 minutes doing it by hand.

RUN A PLATE SOLVE AND SYNC FIRST. This is not optional and it is the single
biggest lesson from building this. An unsynced mount reports that it is on your
target while sitting a degree away, so the field holds no bright star, and every
symptom then looks like a focus or detector problem. Hours went into "fixing"
metrics that were faithfully measuring an empty field. `start_auto_goto` on 4700
does the solve, sync and centre; only then is focusing meaningful.

What makes it fast, each measured on an ASI585MC Air at 1263 mm:

  * USE THE FOCUS PAGE. On `preview` the Air plate-solves and annotates every
    frame — ~6 s of Air-side work — and a `start_exposure` fired during that
    window is SILENTLY DROPPED, so the next download returns the *previous*
    frame. `set_page(["focus"])` has no annotate and the camera free-runs:
    **2.2 s/frame -> 0.35 s/frame**. Note it serves a 1472x831 crop at bin-2
    scale (0.95"/px) regardless of the bin you set.

  * TRUST THE STORED POSITION. True focus is normally within ~100 steps of where
    the EAF was left. Moves cost ~2.1 ms/step, so a 3000-step hop is 6.2 s
    against 0.35 s for a 100-step hop. Sweep +/-400, widen only if the minimum
    is not bracketed.

  * VERIFY EVERY FRAME IS NEW. The header's `imageID` is a constant on this
    firmware and useless for this. Freshness is checked by CONTENT — two real
    exposures never collide. Without it a sweep fills with identical
    consecutive readings and quietly focuses on nothing.

  * SKIP HOT PIXELS. They are bright, compact, and utterly indifferent to
    focus, so locking onto one produces a perfectly flat, meaningless sweep —
    which is exactly what happened. A hot pixel's neighbours sit at background;
    a real star always spills into them.

The focus score is the background-subtracted PEAK, maximised. Total flux is
conserved as focus changes, so concentrating it raises the peak. Star WIDTH is
reported but not used to drive the sweep: the focus page's sampling collapses a
focused star to a couple of pixels, so every width metric tried went flat or
non-monotonic exactly at the bottom of the V (see `measure`).

Normalising peak by aperture flux -- to cancel transparency, and with it the
1.8x swings cloud produces between sweeps -- was implemented and then REMOVED:
measured, it was 13x less reproducible than bare peak, and aperture flux can
come out NEGATIVE when the background ring reads above the aperture interior.
Both failures trace to the same thing, and `measure` spells it out. Real
transparency compensation needs a comparison star, not self-normalisation.

The sweep holds an IDENTITY LOCK on the star: `relocate` takes the brightest
pixel in a window and will silently hand back a neighbour, a hot pixel or a
cosmic ray, producing a plausible curve with a meaningless minimum. Successive
points are seconds apart, so a jump beyond ~30 px is a different object and the
frame is dropped with a log line rather than quietly averaged in.

LIMITATIONS. Single-frame scatter is real — repeat readings of the same star
vary by ~10-20%, so the curve is noisy and the vertex can wander by tens of
steps. In practice the bottom of the V is a ~200-step plateau, and positions
80 steps apart measured identically in an A/B test, so this matters less than
it looks. Needs one moderately bright, unsaturated star in the field; the
brightest star saturating is handled by shortening exposure and then dropping
gain.

EVERY FRAME IS WRITTEN AS IT IS TAKEN. The sweep saves the raw 16-bit buffer
and a star closeup to disk at each step, the moment the step is measured --
not batched up and written at the end. Two reasons, one practical and one
diagnostic. Practically, holding ~30 full frames in memory to write later is
~75 MB of buffers for no reason. Diagnostically, the frames you most want are
the ones from a run that did NOT finish: a sweep that lost the star, saturated,
or was killed halfway used to leave nothing behind at all, which is exactly the
run worth looking at. Frames rejected by the identity lock are written too,
labelled with why. Default output is ./focus-frames/<timestamp>/; --no-images
turns it off and --no-raw keeps the PNGs but drops the bulky .raw dumps.

Progress is logged once a second through anything slow (see airlog.py) --
focuser moves, frame downloads, the pixel scans -- so a stalled sweep is
visible immediately rather than after the timeout.

    python3 focus.py --host <air-ip> --key embedded_key.pem
    python3 focus.py --host <air-ip> --key embedded_key.pem --images shots/
    python3 focus.py --host <air-ip> --key embedded_key.pem --span 1500 --star 520,456
    python3 focus.py --host <air-ip> --key embedded_key.pem -v   # every RPC call
"""

import argparse
import array
import datetime
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from air_rpc import Air
from airlog import add_log_args, configure_logging, get_logger
from main_image import MainImage

log = get_logger("focus")

MAIN_PORT = 4700
SAT = 60000          # per-pixel ADU we treat as saturated (16-bit frames)


class Focuser:
    def __init__(self, air):
        self.air = air
        with log.slow("attach focuser") as tk:
            r = self._r("get_focuser_state")
            log.debug("focuser state: %s", r)
            if r.get("state") == "close":
                tk.event("focuser closed — opening EAF by id 0")
                self._r("open_focuser", [0])          # by id; the name gives 524
                tk.set("waiting 1.5s for the EAF to come up")
                time.sleep(1.5)
                r = self._r("get_focuser_state")
            if r.get("state") == "close":
                raise RuntimeError("no focuser — is the EAF plugged in?")
        log.info("focuser ready at %s (%sC)", self.position(), self.temperature())

    def _r(self, m, p=None):
        rep = self.air.call(m, p or [], timeout=20)
        if isinstance(rep, dict) and rep.get("error"):
            log.error("%s failed: %s (%s)", m, rep["error"], rep.get("code"))
            raise RuntimeError(f"{m}: {rep['error']} ({rep.get('code')})")
        return rep.get("result")

    def position(self):
        return self._r("get_focuser_position")

    def temperature(self):
        return (self._r("get_focuser_info") or {}).get("temperature")

    def move(self, pos, timeout=40.0):
        """Absolute move. Returns once the EAF reports idle (~2.1 ms/step).

        At 2.1 ms/step a 3000-step hop is 6.2 s of silence, so the wait reports
        the live position once a second: a stuck EAF and a long move look
        identical otherwise, right up to the timeout.
        """
        here = self.position()
        pos = int(pos)
        log.debug("move_focuser %s -> %d (%+d steps, ~%.1fs at 2.1ms/step)",
                  here, pos, pos - (here or 0), abs(pos - (here or 0)) * 0.0021)
        self._r("move_focuser", [pos])
        t0 = time.time()
        seen = [here]
        with log.slow("focuser move to %d" % pos, quiet_for=1.0,
                      detail=lambda: "at %s, %+d to go"
                                     % (seen[0], pos - (seen[0] or pos))):
            while time.time() - t0 < timeout:
                time.sleep(0.05)
                if self._r("get_focuser_state").get("state") == "idle":
                    ap = self.position()
                    log.debug("focuser settled at %s in %.2fs", ap, time.time() - t0)
                    return ap
                # Only poll the position on the slow path: it is a second RPC
                # round trip and the fast case does not need it.
                if time.time() - t0 > 1.0:
                    seen[0] = self.position()
        log.error("focuser did not settle within %.0fs (last position %s)",
                  timeout, seen[0])
        raise TimeoutError(f"focuser did not settle within {timeout:.0f}s")


class Frames:
    """Expose on 4700, download from 4800, hand back a row-major u16 buffer."""

    def __init__(self, host, key, cam_name=None, binning=2):
        self.host, self.key = host, key
        self.n_frames = 0
        self.t_download = 0.0
        # Camera setup is a good 5 s of close/open/settle. Tick through it: an
        # open_camera that never returns is the usual sign another client (the
        # phone app, an Alpaca session) is holding the sensor.
        with log.slow("open camera") as tk:
            tk.set("4700 handshake")
            self.air = Air(host, MAIN_PORT, key=key)
            if not self.air.verified:
                raise RuntimeError("4700 handshake failed — check --key")
            tk.set("connecting 4800 image socket")
            self.img = MainImage(host)
            cams = self._call("get_connected_cameras") or []
            log.debug("cameras reported by the Air: %s",
                      ", ".join("%s chip=%s" % (c.get("name"), c.get("chip_size"))
                                for c in cams) or "(none)")
            if cam_name is None:                       # a bare open gets the GUIDE sensor
                best = max(cams, key=lambda c: c.get("chip_size", [0, 0])[0], default=None)
                cam_name = best and best["name"]
                log.info("no --camera given; picked %r (largest chip)", cam_name)
            tk.set("close_camera")
            self._call("close_camera"); time.sleep(1.2)
            tk.set("open_camera %s" % cam_name)
            self._call("open_camera", [cam_name], t=25); time.sleep(1.2)
            self.name = cam_name
            if binning:
                tk.set("set bin %d" % binning)
                self._call("set_camera_bin", [int(binning)]); time.sleep(0.6)
            self.bin = self._call("get_camera_bin") or 1
        self._last_sig = None
        self._cur = None
        # The FOCUS page is the fast lane. On 'preview' the Air plate-solves and
        # annotates every frame (~6 s of Air-side work) and a start_exposure
        # fired during it is silently dropped -- which shows up as "the same
        # frame again". On 'focus' there is no annotate and the camera free-runs
        # at ~0.35 s/frame, so we just watch for the content to change.
        self._call("set_page", ["focus"])
        log.info("camera %s open, bin %s, page=focus (~0.35s/frame)",
                 self.name, self.bin)

    def _call(self, m, p=None, t=20, tries=3):
        for i in range(tries):
            try:
                r = self.air.call(m, p or [], timeout=t)
                return r.get("result", r.get("error"))
            except Exception as e:
                if i == tries - 1:
                    log.error("%s failed %d times, giving up: %s", m, tries, e)
                    raise
                log.warn("%s failed (%s) — reconnecting 4700 and retrying "
                         "(attempt %d/%d)", m, e, i + 2, tries)
                try: self.air.close()
                except Exception: pass
                time.sleep(1.0)
                with log.slow("re-handshake 4700"):
                    self.air = Air(self.host, MAIN_PORT, key=self.key)

    def _fresh(self, timeout=25.0):
        """Return the next frame whose CONTENT differs from the last one.

        Content, not `imageID` -- that header field is a constant on this
        firmware. Two real exposures never collide, the noise alone differs.
        """
        t0 = time.time()
        kicked = False
        stale = [0]
        with log.slow("wait for a fresh frame", quiet_for=1.0,
                      detail=lambda: "%d identical frame(s) so far "
                                     "(camera not looping?)" % stale[0]) as tk:
            while time.time() - t0 < timeout:
                hdr, files = self.img.get_image("get_current_img", 0)
                raw = next(iter(files.values()))
                sig = hash(raw[::4001])
                if sig != self._last_sig:
                    self._last_sig = sig
                    self.n_frames += 1
                    self.t_download += time.time() - t0
                    log.debug("frame #%d %dx%d after %.2fs (%d stale skipped)",
                              self.n_frames, hdr["width"], hdr["height"],
                              time.time() - t0, stale[0])
                    return raw, hdr
                stale[0] += 1
                if not kicked and time.time() - t0 > 4:
                    tk.event("still the same frame after 4s — kicking "
                             "start_exposure")
                    try: self._call("start_exposure")     # nudge if it is not looping
                    except Exception as e: log.warn("kick failed: %s", e)
                    kicked = True
                time.sleep(0.12)
        log.error("no fresh frame within %.0fs (%d identical frames)",
                  timeout, stale[0])
        raise RuntimeError("no fresh frame from the Air")

    def grab(self, exp_s, gain, skip=1):
        """Expose and download.

        `skip` frames are thrown away first. This matters: the camera free-runs,
        so the frame sitting on the Air right after a focuser move was very
        likely started BEFORE the move finished. Discarding one guarantees the
        frame we measure belongs to the position we are measuring.
        """
        if (exp_s, gain) != self._cur:
            log.info("camera -> %.3fs @ gain %d", exp_s, gain)
            self._call("set_control_value", ["Exposure", int(exp_s * 1_000_000)])
            self._call("set_control_value", ["Gain", int(gain)])
            self._cur = (exp_s, gain)
            skip = max(skip, 2)          # a settings change lands a frame later
        t0 = time.time()
        with log.slow("grab %.3fs frame" % exp_s, quiet_for=1.5,
                      detail=lambda: "discarding %d stale frame(s) first" % skip):
            for i in range(skip):
                log.debug("discarding frame %d/%d (free-running camera)", i + 1, skip)
                self._fresh(timeout=max(25.0, exp_s * 3 + 10))
            raw, hdr = self._fresh(timeout=max(25.0, exp_s * 3 + 10))
            buf = array.array("H"); buf.frombytes(raw)
            if sys.byteorder == "big" and not hdr["isBigEndian"]:
                buf.byteswap()
        log.debug("grab done in %.2fs -> %dx%d", time.time() - t0,
                  hdr["width"], hdr["height"])
        return buf, hdr["width"], hdr["height"]

    def close(self):
        log.info("closing camera after %d frame(s)", self.n_frames)
        try:
            self._call("set_page", ["preview"])
        except Exception as e:
            log.warn("could not restore the preview page: %s", e)
        try: self.img.close()
        finally: self.air.close()


# --------------------------------------------------------------------------
# measurement — everything works on a small window, so plain Python is fast
# --------------------------------------------------------------------------

def _median(xs):
    s = sorted(xs); return s[len(s) // 2]


def _mad_sigma(xs, med):
    return 1.4826 * _median([abs(x - med) for x in xs]) or 1.0


def _neighbour_ratio(v, w, h, x, y, bg):
    """Brightest 8-neighbour as a fraction of the centre, background-subtracted.

    A HOT PIXEL is a lone bright pixel whose neighbours sit at background, so
    this is ~0. Any real star — even one undersampled to a single pixel by the
    focus page's 1472x831 downscale — spills measurably into its neighbours.
    Worth the check: a hot pixel is bright, compact and completely indifferent
    to focus, so locking onto one yields a perfectly flat, meaningless sweep.
    """
    c = v[y * w + x] - bg
    if c <= 0:
        return 0.0
    best = 0
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            xx, yy = x + dx, y + dy
            if 0 <= xx < w and 0 <= yy < h:
                best = max(best, v[yy * w + xx] - bg)
    return best / float(c)


def find_star(v, w, h, box=48, grid=32, nsig=8.0, min_ratio=0.15, tries=12):
    """Brightest significant, non-hot, compact source. Returns (x,y,sigma) or None.

    Three hard lessons are baked in:

      * ONLY exclude what cannot be measured. An earlier version trimmed a 12%
        border and threw away the actual focus star (it sat at y=992 of 1080).
      * NEVER return the brightest thing unconditionally. Without a significance
        test this returned a background cell *below the frame median*.
      * SKIP HOT PIXELS. They outshine real stars in a short exposure and never
        change with focus.
    """
    m = box + 8
    cells = []
    rows = max(1, len(range(m, h - m, grid)))
    # This walks every pixel of a 1472x831 frame in pure Python — seconds, not
    # milliseconds, and it happens before anything else has printed. Tick it.
    done = [0]
    with log.slow("scan %dx%d frame for stars" % (w, h), quiet_for=1.0,
                  detail=lambda: "%d/%d rows of cells, %d cells"
                                 % (done[0], rows, len(cells))):
        for gy in range(m, h - m, grid):
            for gx in range(m, w - m, grid):
                best = 0
                for y in range(gy, min(gy + grid, h - m)):
                    row = y * w
                    for x in range(gx, min(gx + grid, w - m)):
                        p = v[row + x]
                        if p > best: best = p
                cells.append((best, gx + grid // 2, gy + grid // 2))
            done[0] += 1
    if not cells:
        log.warn("frame too small to scan (%dx%d, margin %d)", w, h, m)
        return None
    vals = [c[0] for c in cells]
    bg = _median(vals)
    sig = _mad_sigma(vals, bg)
    cells.sort(reverse=True)
    log.debug("%d cells, background %.0f, sigma %.1f, brightest %.0f (%.1f sigma)",
              len(cells), bg, sig, cells[0][0], (cells[0][0] - bg) / sig)
    for rank, (val, gx, gy) in enumerate(cells[:tries]):
        if (val - bg) < nsig * sig:
            log.debug("candidate %d is only %.1f sigma (< %.1f) — stopping",
                      rank + 1, (val - bg) / sig, nsig)
            break                                  # the rest are fainter still
        px, py = relocate(v, w, h, gx, gy, search=grid, box=box)
        ratio = _neighbour_ratio(v, w, h, px, py, bg)
        if ratio < min_ratio:
            log.debug("candidate %d at (%d,%d) rejected: neighbour ratio %.3f "
                      "< %.2f — hot pixel", rank + 1, px, py, ratio, min_ratio)
            continue                               # hot pixel — skip it
        log.info("star at (%d,%d)  %.1f sigma  neighbour ratio %.2f",
                 px, py, (val - bg) / sig, ratio)
        return px, py, (val - bg) / sig
    log.warn("no non-hot star above %.1f sigma in %d candidates", nsig, tries)
    return None


def relocate(v, w, h, cx, cy, search=70, box=48):
    """Re-find the star near (cx,cy). Defocus shifts the apparent centroid and
    the mount drifts, so a position locked at the first frame goes stale."""
    m = box + 8
    x0, x1 = max(m, cx - search), min(w - m, cx + search)
    y0, y1 = max(m, cy - search), min(h - m, cy + search)
    if x0 >= x1 or y0 >= y1:
        return cx, cy
    best = (-1, cx, cy)
    for y in range(y0, y1):
        row = y * w
        for x in range(x0, x1):
            p = v[row + x]
            if p > best[0]: best = (p, x, y)
    return best[1], best[2]


def measure(v, w, h, cx, cy, box=64, ap=48):
    """Focus quality at (cx,cy). LOWER score is better focus.

    Returns (score, peak, width, flux), or "saturated", or None.
    `score` is the negated background-subtracted PEAK; `flux` is returned only
    for the identity check in `sweep`.

    Peak wins on measurement, not on theory. Total flux is conserved as focus
    changes, so concentrating it raises the peak.

    NORMALISING BY FLUX (peak/flux) WAS TRIED AND IS WORSE -- measured, twice.
    The theory is appealing: cloud dims peak and flux together, so the ratio
    should cancel transparency and kill the 1.8x swings seen between sweeps. In
    practice it was 13x LESS reproducible (101.9% run-to-run deviation against
    7.6% for bare peak) and the two passes disagreed on the best position while
    peak agreed with itself. The flaw is that a BACKGROUND-ESTIMATE error is not
    random noise that averages away: it is a systematic offset multiplied by the
    aperture area, so a 2 ADU error moves a 7200-pixel aperture sum by ~14000
    against a star flux of ~120000. Shrinking the aperture (r=12, 24, 48) does
    not rescue it -- all three were worse than peak and disagreed on the answer.
    If transparency compensation is wanted, it needs a COMPARISON STAR in the
    same frame, not a self-normalisation.

    Peak is also used in preference to any star WIDTH, because the Air's focus
    page serves a 1472x831 crop that samples a focused star across ~2 px, so
    every width metric tried went flat or non-monotonic at the bottom of the V:
      * half-flux over the aperture -> inverted V-curve (background-error bound);
      * area above half-maximum     -> quantises into 4,5,6 px;
      * thresholded second moment   -> corner noise dominates via r^2;
      * flood fill from the peak    -> one pixel clears the isophote at focus.
    `width` is still returned, for reporting only.
    """
    x0, x1 = max(0, cx - box), min(w, cx + box)
    y0, y1 = max(0, cy - box), min(h, cy + box)
    ring = []
    for x in range(x0, x1):
        ring.append(v[y0 * w + x]); ring.append(v[(y1 - 1) * w + x])
    for y in range(y0, y1):
        ring.append(v[y * w + x0]); ring.append(v[y * w + x1 - 1])
    bg = _median(ring)
    noise = _mad_sigma(ring, bg)

    ap2 = ap * ap
    peak = 0; flux = 0.0
    for y in range(y0, y1):
        row = y * w; dy2 = (y - cy) ** 2
        if dy2 > ap2:
            continue
        for x in range(x0, x1):
            if (x - cx) ** 2 + dy2 > ap2:
                continue
            p = v[row + x]
            if p > peak: peak = p
            flux += p - bg
    above = peak - bg
    if above < 8.0 * noise:
        return None                       # nothing significantly above the noise
    if peak >= SAT:
        return "saturated"

    half = bg + above / 2.0
    n = 0
    for y in range(y0, y1):
        row = y * w; dy2 = (y - cy) ** 2
        if dy2 > ap2:
            continue
        for x in range(x0, x1):
            if (x - cx) ** 2 + dy2 <= ap2 and v[row + x] >= half:
                n += 1
    width = 2.0 * math.sqrt(n / math.pi) if n else float("nan")
    return -float(above), peak, width, flux


def pick_exposure(frames, gain, start=0.5, star=None, say=print):
    """Find an exposure/gain where the star is detectable but NOT saturated.

    Returns (exp, gain, x, y, width, flux) or (None,)*6. The flux becomes the
    reference for the identity lock in `sweep`.

    Both knobs are needed. Exposure alone is not enough at either end: a very
    bright star (Vega) still saturates at the 2 ms floor, and dropping gain is
    the only way down.
    """
    exp = start
    lo_exp = 0.002
    log.info("hunting an exposure: starting at %.3fs gain %d%s", exp, gain,
             " (star hint %s)" % (star,) if star else "")
    for attempt in range(14):
        log.debug("exposure attempt %d/14: %.3fs gain %d", attempt + 1, exp, gain)
        v, w, h = frames.grab(exp, gain)
        if star:
            cx, cy = relocate(v, w, h, *star)
        else:
            found = find_star(v, w, h)
            if found is None:
                if exp >= 8:
                    say("  no star detected even at 8 s")
                    return (None,) * 6
                exp = min(8.0, exp * 3.0); continue
            cx, cy = found[0], found[1]
        r = measure(v, w, h, cx, cy)
        if r == "saturated":
            if exp > lo_exp:
                log.info("  saturated at %.3fs — shortening to %.3fs",
                         exp, max(lo_exp, exp / 3.0))
                exp = max(lo_exp, exp / 3.0)
            elif gain > 0:
                gain = max(0, gain - 100)        # at the floor: turn gain down
                say("  saturated at %.3fs — dropping gain to %d" % (exp, gain))
            else:
                say("  star saturates at %.3fs / gain 0 — pick a fainter one "
                    "with --star X,Y" % exp)
                return (None,) * 6
            continue
        if r is None:
            if exp >= 8:
                say("  star found but not measurable at 8 s")
                return (None,) * 6
            exp = min(8.0, exp * 3.0); continue
        log.info("exposure settled: %.3fs gain %d, star (%d,%d) peak %d "
                 "width %.2f px", exp, gain, cx, cy, r[1], r[2])
        return exp, gain, cx, cy, r[2], r[3]
    log.warn("gave up hunting an exposure after 14 attempts")
    return (None,) * 6


def crop_png(v, w, h, cx, cy, path, half=60, label=None):
    """Save a closeup of the focus star. PIL only — imported lazily so the
    core routine stays stdlib-only."""
    from PIL import Image, ImageDraw
    t0 = time.time()
    x0, x1 = max(0, cx - half), min(w, cx + half)
    y0, y1 = max(0, cy - half), min(h, cy + half)
    px = [v[y * w + x] for y in range(y0, y1) for x in range(x0, x1)]
    lo = _median(px); hi = max(px)
    rng = max(hi - lo, 1)
    img = Image.new("L", (x1 - x0, y1 - y0))
    img.putdata([min(255, int(255 * (max(0.0, (p - lo) / rng)) ** 0.5)) for p in px])
    img = img.resize((240, 240), Image.NEAREST).convert("RGB")
    if label:
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 239, 16], fill=(0, 0, 0))
        d.text((4, 4), label, fill=(255, 255, 0))
    img.save(path)
    log.debug("wrote %s (%s) in %.2fs", path,
              _size(path), time.time() - t0)
    return path


def _size(path):
    try:
        n = os.path.getsize(path)
    except OSError:
        return "?"
    return "%.1f KB" % (n / 1024.0) if n < 1024 * 1024 else "%.1f MB" % (n / 1048576.0)


class StepWriter:
    """Write each focus frame to disk AT THE STEP, not at the end of the sweep.

    The old code appended full frame buffers to a list and rendered PNGs after
    the run. That is wrong twice over. It holds ~2.4 MB per step in memory for
    no benefit, and — the reason this class exists — it produces nothing at all
    when a run does not reach the end. A sweep that lost the star at step 6, or
    that was interrupted, is exactly the sweep whose frames you want, and those
    were the runs that left an empty directory behind.

    So every frame is committed as it is measured, including the ones the
    identity lock REJECTS, with the rejection reason in the filename. Three
    files per step:

        step007.raw   the 16-bit row-major buffer, exactly as measured
        step007.json  width/height, star position, position/peak/width, reason
        step007.png   a 240x240 closeup of the tracked star (needs PIL)

    The .raw dump is the bulky one (~2.4 MB a step); --no-raw drops it and keeps
    the rest. A PNG failure is logged and swallowed — losing a preview must
    never abort a sweep that is otherwise fine.
    """

    def __init__(self, outdir, raw=True, half=60):
        self.dir = outdir
        self.raw = raw
        self.half = half
        self.n = 0
        self.bytes = 0
        self.paths = []                 # PNGs, in order, for the contact sheet
        self._png_broken = False
        os.makedirs(outdir, exist_ok=True)
        log.info("writing every focus frame to %s/ as it is taken%s",
                 outdir, "" if raw else " (PNG only, --no-raw)")

    def step(self, v, w, h, cx, cy, label, meta=None):
        """Commit one frame. Returns the base path (no extension)."""
        self.n += 1
        base = os.path.join(self.dir, "step%03d" % self.n)
        t0 = time.time()
        if self.raw:
            # Straight to disk, no buffering across steps: if the process dies
            # in the next second this frame is still on the card.
            with open(base + ".raw", "wb") as f:
                f.write(v.tobytes())
                f.flush()
                os.fsync(f.fileno())
            self.bytes += os.path.getsize(base + ".raw")
        rec = {"step": self.n, "label": label, "width": w, "height": h,
               "star_x": cx, "star_y": cy, "itemsize": v.itemsize,
               "dtype": "uint16", "order": "row-major",
               "time": datetime.datetime.now().isoformat(timespec="seconds")}
        clash = set(rec) & set(meta or {})
        if clash:                      # geometry must survive the caller's meta
            log.warn("sidecar meta overrides %s — renaming to meta_*",
                     ", ".join(sorted(clash)))
            meta = {("meta_" + k if k in clash else k): val
                    for k, val in (meta or {}).items()}
        rec.update(meta or {})
        with open(base + ".json", "w") as f:
            json.dump(rec, f, indent=1)
        if not self._png_broken:
            try:
                self.paths.append(crop_png(v, w, h, cx, cy, base + ".png",
                                           half=self.half, label=label))
            except Exception as e:                  # PIL missing, odd geometry
                self._png_broken = True
                log.warn("PNG closeups disabled (%s); .raw/.json still written", e)
        log.info("step %03d written: %s  [%s]", self.n, base, label)
        log.debug("  wrote in %.2fs (%s total so far)", time.time() - t0,
                  "%.1f MB" % (self.bytes / 1048576.0))
        return base

    def contact_sheet(self, name="sweep.jpg", cols=6):
        """Optional montage of everything written. Purely a convenience — the
        per-step files are already on disk and are the real output."""
        if not self.paths:
            return None
        try:
            from PIL import Image
        except Exception as e:
            log.warn("no montage: %s", e)
            return None
        with log.slow("building contact sheet of %d frame(s)" % len(self.paths),
                      quiet_for=1.0):
            cols = min(cols, len(self.paths))
            rows = (len(self.paths) + cols - 1) // cols
            sheet = Image.new("RGB", (cols * 244, rows * 244), (12, 12, 14))
            for i, path in enumerate(self.paths):
                try:
                    sheet.paste(Image.open(path),
                                ((i % cols) * 244 + 2, (i // cols) * 244 + 2))
                except Exception as e:
                    log.warn("skipping %s in the montage: %s", path, e)
            out = os.path.join(self.dir, name)
            sheet.save(out, quality=92)
        log.info("contact sheet -> %s (%s)", out, _size(out))
        return out


def sweep(frames, foc, centre, span, step, exp, gain, cx, cy,
          say=print, writer=None, tag="", max_jump=30):
    """Sweep the focuser and score each position. Returns [(pos, score, peak)].

    Carries an IDENTITY LOCK on the star, because `relocate` simply takes the
    brightest pixel in a search window and will happily hand back a *different*
    source — a neighbour, a hot pixel, a cosmic ray — without any complaint. A
    sweep that silently changes star mid-way produces a plausible-looking curve
    with a meaningless minimum. Two cheap invariants catch it:

    POSITION CONTINUITY is the check. Successive points are seconds apart, so
    the star cannot really move far; a jump beyond `max_jump` px is a different
    object, not tracking.

    A flux-continuity check was written alongside it and REMOVED, because
    aperture flux is not a trustworthy identity: the background ring can read
    above the aperture interior, which makes the summed flux come out NEGATIVE.
    The tolerance interval then inverts and rejects every frame, including
    perfectly good ones — it did exactly that in testing. The same unreliability
    is why normalising peak by flux fails (see `measure`). Do not reintroduce a
    flux-based test without first checking the sign.

    Rejected frames are logged and skipped rather than silently averaged in.

    `writer`, when given, commits every frame to disk AS THE STEP HAPPENS —
    including rejected ones, tagged with the reason. See StepWriter.
    """
    out = []
    last = (cx, cy)
    positions = list(range(centre - span, centre + span + 1, step))
    done = [0]
    t0 = time.time()
    log.info("sweep %d..%d step %d — %d position(s), est. %.0fs",
             positions[0], positions[-1], step, len(positions),
             len(positions) * (abs(step) * 0.0021 + 1.0))
    # The sweep is the multi-minute part of a focus run, so it reports its own
    # progress every second on top of the per-operation tickers underneath it.
    with log.slow("sweep%s" % (" " + tag if tag else ""), quiet_for=1.0,
                  detail=lambda: "%d/%d positions, %d usable, %.0fs elapsed"
                                 % (done[0], len(positions), len(out),
                                    time.time() - t0)) as tk:
        for p in positions:
            done[0] += 1
            tk.set("position %d (%d/%d)" % (p, done[0], len(positions)))
            log.info("--- step %d/%d: focuser -> %d ---",
                     done[0], len(positions), p)
            ap = foc.move(p)
            v, w, h = frames.grab(exp, gain)
            nx, ny = relocate(v, w, h, last[0], last[1])
            jump = math.hypot(nx - last[0], ny - last[1])

            # One exit per outcome, and every one of them writes the frame
            # first: a rejected frame is the most useful thing on the disk.
            def commit(label, meta=None):
                if writer is not None:
                    writer.step(v, w, h, nx, ny, label, meta)

            base = {"focuser": ap, "requested": p, "exposure_s": exp,
                    "gain": gain, "jump_px": round(jump, 1)}
            if jump > max_jump:
                log.warn("step %d REJECTED: tracked source jumped %.0f px "
                         "(> %d) — different object, not tracking",
                         done[0], jump, max_jump)
                commit("%s%d REJECT jump%.0fpx" % (tag, ap, jump),
                       dict(base, rejected="jump"))
                say("  %7d   (tracked source jumped %.0f px — skipped)" % (ap, jump))
                continue
            r = measure(v, w, h, nx, ny)
            if r == "saturated":
                log.warn("step %d REJECTED: saturated at %.3fs gain %d",
                         done[0], exp, gain)
                commit("%s%d REJECT saturated" % (tag, ap),
                       dict(base, rejected="saturated"))
                say("  %7d   (saturated)" % ap); continue
            if r is None:
                log.warn("step %d REJECTED: nothing above the noise at (%d,%d)",
                         done[0], nx, ny)
                commit("%s%d REJECT no-star" % (tag, ap),
                       dict(base, rejected="no_star"))
                say("  %7d   (no usable star)" % ap); continue
            score, peak, width, flux = r
            last = (nx, ny)
            out.append((ap, score, peak))
            log.info("step %d/%d  focuser %d  peak %d  width %.2f px  "
                     "flux %.0f  (star moved %.1f px)", done[0], len(positions),
                     ap, peak, width, flux, jump)
            commit("%s%d pk%d w%.1f" % (tag, ap, peak, width),
                   dict(base, peak=peak, star_width_px=round(width, 2),
                        flux=round(flux, 1), score=round(score, 1)))
            say("  %7d   peak %7d   width %5.2f px" % (ap, peak, width))
    log.info("sweep finished: %d/%d positions usable in %.0fs",
             len(out), len(positions), time.time() - t0)
    return out


def vertex(pts):
    """Parabola vertex through the minimum and its two neighbours."""
    pts = sorted(pts)
    i = min(range(len(pts)), key=lambda k: pts[k][1])
    if not (0 < i < len(pts) - 1):
        return pts[i][0]
    (x0, y0), (x1, y1), (x2, y2) = [(pts[k][0], pts[k][1]) for k in (i - 1, i, i + 1)]
    den = (x0 - x1) * (x0 - x2) * (x1 - x2)
    if den == 0: return pts[i][0]
    A = (x2 * (y1 - y0) + x1 * (y0 - y2) + x0 * (y2 - y1)) / den
    B = (x2 * x2 * (y0 - y1) + x1 * x1 * (y2 - y0) + x0 * x0 * (y1 - y2)) / den
    return int(round(-B / (2 * A))) if A > 0 else pts[i][0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ)
    ap.add_argument("--key", default="embedded_key.pem")
    ap.add_argument("--camera", default=None, help="main camera name (else the biggest chip)")
    ap.add_argument("--gain", type=int, default=100)
    ap.add_argument("--bin", type=int, default=2, help="2 halves the download; 0 leaves it alone")
    ap.add_argument("--span", type=int, default=400, help="+/- steps for the coarse pass")
    ap.add_argument("--step", type=int, default=100)
    ap.add_argument("--widen", type=int, default=3, help="times to double the span on failure")
    ap.add_argument("--images", metavar="DIR", default=None,
                    help="where to write each step's frame as it is taken "
                         "(default: focus-frames/<timestamp>/)")
    ap.add_argument("--no-images", action="store_true",
                    help="do not write per-step frames to disk at all")
    ap.add_argument("--no-raw", action="store_true",
                    help="write the PNG closeups but not the ~2.4 MB .raw dumps")
    ap.add_argument("--star", metavar="X,Y", help="use this star instead of auto-picking")
    add_log_args(ap)
    a = ap.parse_args()
    configure_logging(a)

    t_start = time.time()
    # Frames are written as they are taken, so the directory is created up
    # front — before the first exposure, not after the last.
    writer = None
    if not a.no_images:
        outdir = a.images or os.path.join(
            "focus-frames", datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        writer = StepWriter(outdir, raw=not a.no_raw)
    else:
        log.info("--no-images: no frames will be written to disk")

    log.info("focus run starting: host=%s camera=%s bin=%s gain=%d span=+/-%d "
             "step=%d", a.host, a.camera or "(auto)", a.bin, a.gain, a.span, a.step)
    frames = Frames(a.host, a.key, a.camera, binning=a.bin)
    foc = Focuser(frames.air)
    start = foc.position()
    print(f"camera {frames.name} bin {frames.bin} | focuser at {start} "
          f"({foc.temperature()}C)")

    hint = tuple(int(t) for t in a.star.split(",")) if a.star else None
    exp, gain, cx, cy, h0, _flux = pick_exposure(frames, a.gain, star=hint)
    if exp is None:
        log.error("no usable star found after the exposure hunt")
        print("no usable star found — sky clear? scope uncovered? try --star X,Y",
              file=sys.stderr)
        frames.close()
        return 1
    print(f"star at ({cx},{cy})  exposure {exp:.3f}s gain {gain}  "
          f"width {h0:.2f} px at the stored position\n")

    span, pts = a.span, []
    for attempt in range(a.widen + 1):
        print(f"coarse sweep +/-{span} step {a.step}:")
        log.info("coarse pass %d/%d: +/-%d step %d", attempt + 1, a.widen + 1,
                 span, a.step)
        pts = sweep(frames, foc, start, span, a.step, exp, gain, cx, cy,
                    writer=writer, tag="c%d " % (attempt + 1))
        if len(pts) >= 3:
            best = min(pts, key=lambda r: r[1])
            if best[0] not in (pts[0][0], pts[-1][0]):
                log.info("minimum bracketed at %d — coarse pass done", best[0])
                break                                  # minimum is bracketed
            log.info("minimum sits at the edge (%d) — doubling the span to %d",
                     best[0], span * 2)
            print("  minimum at the edge — widening")
        else:
            log.warn("only %d usable point(s) — widening to +/-%d", len(pts), span * 2)
        span *= 2

    if len(pts) < 3:
        log.error("sweep failed: %d usable point(s) after %d pass(es)",
                  len(pts), a.widen + 1)
        print("sweep failed to find a focus curve", file=sys.stderr)
        frames.close()
        return 1

    v0 = vertex(pts)
    print(f"\ncoarse minimum -> {v0}; fine sweep +/-{a.step} step {max(10, a.step//5)}:")
    log.info("coarse vertex %d; fine sweep +/-%d step %d", v0, a.step,
             max(10, a.step // 5))
    fine = sweep(frames, foc, v0, a.step, max(10, a.step // 5), exp, gain, cx, cy,
                 writer=writer, tag="f ")
    best_pos = vertex(fine) if len(fine) >= 3 else v0
    if len(fine) < 3:
        log.warn("fine sweep gave only %d point(s) — keeping the coarse vertex %d",
                 len(fine), v0)

    log.info("settling on %d", best_pos)
    foc.move(best_pos)
    v, w, h = frames.grab(exp, gain)
    cx, cy = relocate(v, w, h, cx, cy)
    r = measure(v, w, h, cx, cy)
    hf = r[2] if isinstance(r, tuple) else float("nan")
    elapsed = time.time() - t_start
    if writer is not None:
        writer.step(v, w, h, cx, cy, "FINAL %d w%.1f" % (best_pos, hf),
                    {"focuser": best_pos, "final": True,
                     "star_width_px": None if hf != hf else round(hf, 2),
                     "elapsed_s": round(elapsed, 1)})
    print(f"\nFOCUS {best_pos}  width {hf:.2f} px  ({foc.temperature()}C)  "
          f"in {elapsed:.0f}s")
    log.info("FOCUS %d  width %.2f px  %sC  in %.0fs  (%d frames, %d written)",
             best_pos, hf, foc.temperature(), elapsed, frames.n_frames,
             writer.n if writer else 0)

    if writer is not None:
        sheet = writer.contact_sheet()
        if sheet:
            print("closeups -> %s (%d frames)" % (sheet, len(writer.paths)))
        print("per-step frames -> %s/ (%d steps, %.1f MB)"
              % (writer.dir, writer.n, writer.bytes / 1048576.0))

    frames.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        log.error("%s", e)
        print("ERROR:", e, file=sys.stderr); sys.exit(1)
    except KeyboardInterrupt:
        # Frames are already on disk step by step, so an interrupted sweep
        # still leaves everything it measured behind.
        log.warn("interrupted — frames written so far are on disk")
        sys.exit(130)
