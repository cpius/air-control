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

    python3 focus.py --host <air-ip> --key embedded_key.pem
    python3 focus.py --host <air-ip> --key embedded_key.pem --images shots/
    python3 focus.py --host <air-ip> --key embedded_key.pem --span 1500 --star 520,456
"""

import argparse
import array
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from air_rpc import Air
from main_image import MainImage

MAIN_PORT = 4700
SAT = 60000          # per-pixel ADU we treat as saturated (16-bit frames)


class Focuser:
    def __init__(self, air):
        self.air = air
        r = self._r("get_focuser_state")
        if r.get("state") == "close":
            self._r("open_focuser", [0])          # by id; the name gives 524
            time.sleep(1.5)
            r = self._r("get_focuser_state")
        if r.get("state") == "close":
            raise RuntimeError("no focuser — is the EAF plugged in?")

    def _r(self, m, p=None):
        rep = self.air.call(m, p or [], timeout=20)
        if isinstance(rep, dict) and rep.get("error"):
            raise RuntimeError(f"{m}: {rep['error']} ({rep.get('code')})")
        return rep.get("result")

    def position(self):
        return self._r("get_focuser_position")

    def temperature(self):
        return (self._r("get_focuser_info") or {}).get("temperature")

    def move(self, pos, timeout=40.0):
        """Absolute move. Returns once the EAF reports idle (~2.1 ms/step)."""
        self._r("move_focuser", [int(pos)])
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(0.05)
            if self._r("get_focuser_state").get("state") == "idle":
                return self.position()
        raise TimeoutError(f"focuser did not settle within {timeout:.0f}s")


class Frames:
    """Expose on 4700, download from 4800, hand back a row-major u16 buffer."""

    def __init__(self, host, key, cam_name=None, binning=2):
        self.host, self.key = host, key
        self.air = Air(host, MAIN_PORT, key=key)
        if not self.air.verified:
            raise RuntimeError("4700 handshake failed — check --key")
        self.img = MainImage(host)
        cams = self._call("get_connected_cameras") or []
        if cam_name is None:                       # a bare open gets the GUIDE sensor
            best = max(cams, key=lambda c: c.get("chip_size", [0, 0])[0], default=None)
            cam_name = best and best["name"]
        self._call("close_camera"); time.sleep(1.2)
        self._call("open_camera", [cam_name], t=25); time.sleep(1.2)
        self.name = cam_name
        if binning:
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

    def _call(self, m, p=None, t=20, tries=3):
        for i in range(tries):
            try:
                r = self.air.call(m, p or [], timeout=t)
                return r.get("result", r.get("error"))
            except Exception:
                if i == tries - 1:
                    raise
                try: self.air.close()
                except Exception: pass
                time.sleep(1.0)
                self.air = Air(self.host, MAIN_PORT, key=self.key)

    def _fresh(self, timeout=25.0):
        """Return the next frame whose CONTENT differs from the last one.

        Content, not `imageID` -- that header field is a constant on this
        firmware. Two real exposures never collide, the noise alone differs.
        """
        t0 = time.time()
        kicked = False
        while time.time() - t0 < timeout:
            hdr, files = self.img.get_image("get_current_img", 0)
            raw = next(iter(files.values()))
            sig = hash(raw[::4001])
            if sig != self._last_sig:
                self._last_sig = sig
                return raw, hdr
            if not kicked and time.time() - t0 > 4:
                try: self._call("start_exposure")     # nudge if it is not looping
                except Exception: pass
                kicked = True
            time.sleep(0.12)
        raise RuntimeError("no fresh frame from the Air")

    def grab(self, exp_s, gain, skip=1):
        """Expose and download.

        `skip` frames are thrown away first. This matters: the camera free-runs,
        so the frame sitting on the Air right after a focuser move was very
        likely started BEFORE the move finished. Discarding one guarantees the
        frame we measure belongs to the position we are measuring.
        """
        if (exp_s, gain) != self._cur:
            self._call("set_control_value", ["Exposure", int(exp_s * 1_000_000)])
            self._call("set_control_value", ["Gain", int(gain)])
            self._cur = (exp_s, gain)
            skip = max(skip, 2)          # a settings change lands a frame later
        for _ in range(skip):
            self._fresh(timeout=max(25.0, exp_s * 3 + 10))
        raw, hdr = self._fresh(timeout=max(25.0, exp_s * 3 + 10))
        buf = array.array("H"); buf.frombytes(raw)
        if sys.byteorder == "big" and not hdr["isBigEndian"]:
            buf.byteswap()
        return buf, hdr["width"], hdr["height"]

    def close(self):
        try:
            self._call("set_page", ["preview"])
        except Exception:
            pass
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
    for gy in range(m, h - m, grid):
        for gx in range(m, w - m, grid):
            best = 0
            for y in range(gy, min(gy + grid, h - m)):
                row = y * w
                for x in range(gx, min(gx + grid, w - m)):
                    p = v[row + x]
                    if p > best: best = p
            cells.append((best, gx + grid // 2, gy + grid // 2))
    if not cells:
        return None
    vals = [c[0] for c in cells]
    bg = _median(vals)
    sig = _mad_sigma(vals, bg)
    cells.sort(reverse=True)
    for val, gx, gy in cells[:tries]:
        if (val - bg) < nsig * sig:
            break                                  # the rest are fainter still
        px, py = relocate(v, w, h, gx, gy, search=grid, box=box)
        if _neighbour_ratio(v, w, h, px, py, bg) < min_ratio:
            continue                               # hot pixel — skip it
        return px, py, (val - bg) / sig
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


def pick_exposure(frames, gain, start=0.5, star=None, log=print):
    """Find an exposure/gain where the star is detectable but NOT saturated.

    Returns (exp, gain, x, y, width, flux) or (None,)*6. The flux becomes the
    reference for the identity lock in `sweep`.

    Both knobs are needed. Exposure alone is not enough at either end: a very
    bright star (Vega) still saturates at the 2 ms floor, and dropping gain is
    the only way down.
    """
    exp = start
    lo_exp = 0.002
    for _ in range(14):
        v, w, h = frames.grab(exp, gain)
        if star:
            cx, cy = relocate(v, w, h, *star)
        else:
            found = find_star(v, w, h)
            if found is None:
                if exp >= 8:
                    log("  no star detected even at 8 s")
                    return (None,) * 6
                exp = min(8.0, exp * 3.0); continue
            cx, cy = found[0], found[1]
        r = measure(v, w, h, cx, cy)
        if r == "saturated":
            if exp > lo_exp:
                exp = max(lo_exp, exp / 3.0)
            elif gain > 0:
                gain = max(0, gain - 100)        # at the floor: turn gain down
                log("  saturated at %.3fs — dropping gain to %d" % (exp, gain))
            else:
                log("  star saturates at %.3fs / gain 0 — pick a fainter one "
                    "with --star X,Y" % exp)
                return (None,) * 6
            continue
        if r is None:
            if exp >= 8:
                log("  star found but not measurable at 8 s")
                return (None,) * 6
            exp = min(8.0, exp * 3.0); continue
        return exp, gain, cx, cy, r[2], r[3]
    return (None,) * 6


def crop_png(v, w, h, cx, cy, path, half=60, label=None):
    """Save a closeup of the focus star. PIL only — imported lazily so the
    core routine stays stdlib-only."""
    from PIL import Image, ImageDraw
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
    return path


def sweep(frames, foc, centre, span, step, exp, gain, cx, cy,
          log=print, shots=None, tag="", max_jump=30):
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
    """
    out = []
    last = (cx, cy)
    for p in range(centre - span, centre + span + 1, step):
        ap = foc.move(p)
        v, w, h = frames.grab(exp, gain)
        nx, ny = relocate(v, w, h, last[0], last[1])
        jump = math.hypot(nx - last[0], ny - last[1])
        if jump > max_jump:
            log("  %7d   (tracked source jumped %.0f px — skipped)" % (ap, jump))
            continue
        r = measure(v, w, h, nx, ny)
        if r == "saturated":
            log("  %7d   (saturated)" % ap); continue
        if r is None:
            log("  %7d   (no usable star)" % ap); continue
        score, peak, width, flux = r
        last = (nx, ny)
        out.append((ap, score, peak))
        log("  %7d   peak %7d   width %5.2f px" % (ap, peak, width))
        if shots is not None:
            shots.append((v, w, h, nx, ny,
                          "%s%d pk%d w%.1f" % (tag, ap, peak, width)))
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
    ap.add_argument("--images", metavar="DIR",
                    help="write a closeup of the focus star at every step (needs PIL)")
    ap.add_argument("--star", metavar="X,Y", help="use this star instead of auto-picking")
    a = ap.parse_args()

    t_start = time.time()
    frames = Frames(a.host, a.key, a.camera, binning=a.bin)
    foc = Focuser(frames.air)
    start = foc.position()
    print(f"camera {frames.name} bin {frames.bin} | focuser at {start} "
          f"({foc.temperature()}C)")

    hint = tuple(int(t) for t in a.star.split(",")) if a.star else None
    exp, gain, cx, cy, h0, _flux = pick_exposure(frames, a.gain, star=hint)
    if exp is None:
        print("no usable star found — sky clear? scope uncovered? try --star X,Y",
              file=sys.stderr)
        frames.close()
        return 1
    print(f"star at ({cx},{cy})  exposure {exp:.3f}s gain {gain}  "
          f"width {h0:.2f} px at the stored position\n")

    shots = [] if a.images else None
    if a.images:
        os.makedirs(a.images, exist_ok=True)

    span, pts = a.span, []
    for attempt in range(a.widen + 1):
        print(f"coarse sweep +/-{span} step {a.step}:")
        pts = sweep(frames, foc, start, span, a.step, exp, gain, cx, cy,
                    shots=shots, tag="")
        if len(pts) >= 3:
            best = min(pts, key=lambda r: r[1])
            if best[0] not in (pts[0][0], pts[-1][0]):
                break                                  # minimum is bracketed
            print("  minimum at the edge — widening")
        span *= 2

    if len(pts) < 3:
        print("sweep failed to find a focus curve", file=sys.stderr)
        frames.close()
        return 1

    v0 = vertex(pts)
    print(f"\ncoarse minimum -> {v0}; fine sweep +/-{a.step} step {max(10, a.step//5)}:")
    fine = sweep(frames, foc, v0, a.step, max(10, a.step // 5), exp, gain, cx, cy,
                 shots=shots, tag="")
    best_pos = vertex(fine) if len(fine) >= 3 else v0

    foc.move(best_pos)
    v, w, h = frames.grab(exp, gain)
    cx, cy = relocate(v, w, h, cx, cy)
    r = measure(v, w, h, cx, cy)
    hf = r[2] if isinstance(r, tuple) else float("nan")
    elapsed = time.time() - t_start
    print(f"\nFOCUS {best_pos}  width {hf:.2f} px  ({foc.temperature()}C)  "
          f"in {elapsed:.0f}s")

    if a.images:
        shots.append((v, w, h, cx, cy, "FINAL %d w%.1f" % (best_pos, hf)))
        paths = []
        for i, (vv, ww, hh, sx, sy, lab) in enumerate(shots):
            paths.append(crop_png(vv, ww, hh, sx, sy,
                                  os.path.join(a.images, "step%02d.png" % i), label=lab))
        try:
            from PIL import Image
            cols = min(6, len(paths)); rows = (len(paths) + cols - 1) // cols
            sheet = Image.new("RGB", (cols * 244, rows * 244), (12, 12, 14))
            for i, p in enumerate(paths):
                sheet.paste(Image.open(p), ((i % cols) * 244 + 2, (i // cols) * 244 + 2))
            sheet.save(os.path.join(a.images, "sweep.jpg"), quality=92)
            print("closeups -> %s/sweep.jpg (%d frames)" % (a.images, len(paths)))
        except Exception as e:
            print("montage failed:", e, file=sys.stderr)

    frames.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print("ERROR:", e, file=sys.stderr); sys.exit(1)
