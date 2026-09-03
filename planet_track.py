#!/usr/bin/env python3
"""Keep a planet on the sensor centre at ~1 Hz with joystick pulses -- and
keep doing it WHILE the Air records video.

Why not goto: a goto of a few arcseconds never reports completion on this
firmware (120 s timeout per step) and lands ~1' off anyway. Why not the
guider: it wants stars. `scope_move` on 4400 is relative, works when the
pointing model is garbage, and a timed pulse at 1x-4x sidereal is a 3"-60"
nudge -- the same thing PHD2 does, without PHD2.

Frame sources (--source):
  main   the main camera over 4800: focus page (1:1 centre crop, ~0.4 s) or
         the video page ("rtmp") with an ROI. Verified 2026-09-02: on the rtmp
         page the camera free-runs at 40 fps, start_record_avi writes to eMMC,
         and 4800 keeps serving frames at ~3 Hz DURING the recording -- so the
         loop can centre on the very frames being recorded (--video-roi + --record).
  guide  the ON-AXIS guide sensor over 4500: 640x360 8-bit at ~1 fps, fully
         independent of the main camera, so recording cannot starve it.
         The guide field is offset from the main field: measure once where the
         planet sits on the guide sensor when it is centred on the main sensor
         (--where prints both) and pass that as --target.

Calibration is done in situ every run (direction signs flip with pier side
and the slew rate table is not to be trusted): one pulse east, one north,
measured in pixels. The loop corrects any residual because it measures again
every cycle.

    python3 planet_track.py --source main --page focus --bin 2 --exp 0.05 --gain 100 --seconds 120
    python3 planet_track.py --source main --video-roi 512x512 --exp 0.015 --gain 250 --record 90
    python3 planet_track.py --source guide --target 331,190 --seconds 600
    python3 planet_track.py --where              # planet position on both sensors

Proven 2026-09-02 on Saturn: 512x512 ROI at 15 ms/gain 250 records at 49 fps
while the loop runs at 2.8 Hz, median hold 9 px (4"), worst 17 px. Small ROI is
the whole game: the eMMC writes ~15-20 Mpx/s whatever the exposure.

Ctrl-C is safe: the finally block sends scope_move ["none"], restores the
slew rate, and stops any recording it started. An un-stopped joystick keeps
slewing -- that is the one thing this script must never leave behind.
"""
import argparse
import json
import math
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session import Session
from air_rpc import Air

DIRS = {"ra": ("east", "west"), "dec": ("north", "south")}
CHIP = (3840, 2160)


# ---------------------------------------------------------------- detection
def find_planet(img, min_px=12, min_signal=None):
    """Centroid of the brightest extended blob. A single hot pixel at full
    scale must not win, so we require an area, and we cluster around the
    median of the bright pixels so a second bright thing does not pull it."""
    img = img.astype(np.int32)
    bg = int(np.median(img[::7, ::7]))
    peak = int(img.max())
    if min_signal is None:
        min_signal = 25 if peak <= 255 else 400
    if peak - bg < min_signal:
        return None, "no signal (peak %d over bg %d)" % (peak, bg)
    thr = bg + max(0.5 * (peak - bg), 0.6 * min_signal)
    ys, xs = np.nonzero(img >= thr)
    if len(xs) < min_px:
        return None, "blob too small (%d px)" % len(xs)
    mx, my = np.median(xs), np.median(ys)
    m = (np.abs(xs - mx) < 200) & (np.abs(ys - my) < 200)
    xs, ys = xs[m], ys[m]
    if len(xs) < min_px:
        return None, "blob too small after clustering"
    wts = (img[ys, xs] - bg).astype(float)
    return (float((xs * wts).sum() / wts.sum()), float((ys * wts).sum() / wts.sum())), \
        "peak %d bg %d area %d" % (peak, bg, len(xs))


# ---------------------------------------------------------------- frame sources
class MainFrames:
    """Main camera over 4800, through Session (content-fresh frames)."""

    def __init__(self, s, page, exp_s, gain, binning=1, roi=None, settle=0.15):
        self.s, self.exp, self.settle = s, exp_s, settle
        s.c("stop_exposure"); time.sleep(0.6)
        if page:
            r = s.c("set_page", [page])
            print("set_page %s -> %s" % (page, r), flush=True)
        if roi:
            # Order matters (2026-09-02): set_page("rtmp") RESETS the subframe
            # to full frame and auto-starts capture, so the ROI has to be set
            # after the page switch, with the capture stopped first, and
            # verified -- two of tonight's clips silently recorded 1920x1080.
            w, h = roi
            want = {"x": (CHIP[0] - w) // 2, "y": (CHIP[1] - h) // 2, "width": w, "height": h}
            for attempt in range(4):
                s.c("stop_exposure"); time.sleep(0.6)
                s.c("set_camera_bin", [1])
                r = s.c("set_subframe", [want])
                got = s.c("get_subframe")
                if isinstance(got, dict) and got.get("width") == w and got.get("height") == h:
                    break
                print("subframe attempt %d: set -> %s, got %s" % (attempt + 1, r, got), flush=True)
                time.sleep(0.5)
            else:
                raise RuntimeError("could not apply the %dx%d subframe: %s" % (w, h, got))
            print("subframe:", got, flush=True)
        s.c("set_camera_bin", [int(binning)])
        s.set_exp(exp_s, gain)
        s.c("start_exposure", ["light"] if roi else [])
        time.sleep(exp_s + 1.0)

    def get(self):
        v, w, h = self.s.fresh(1)
        # Session hands back the raw bytes as an array('H'). The focus/preview
        # pages send 16-bit pixels; the rtmp (video) page sends 8-bit ones, so
        # the same byte buffer is w*h bytes instead of 2*w*h.
        raw = np.frombuffer(v, dtype=np.uint8)
        if raw.size == w * h:
            img = raw.reshape(h, w)
        elif raw.size == 2 * w * h:
            img = raw.view(np.uint16).reshape(h, w)
        else:
            raise RuntimeError("frame %dx%d but %d bytes" % (w, h, raw.size))
        return img, w, h

    def after_move(self):
        time.sleep(self.exp + self.settle)
        return self.get()

    def close(self):
        """Put the readout back to full frame. The rtmp page restarts capture
        right after stop_exposure, so the reset needs a pause and a check --
        without it the 512x512 subframe was still set the next morning."""
        full = {"x": 0, "y": 0, "width": CHIP[0], "height": CHIP[1]}
        for _ in range(4):
            try:
                self.s.c("stop_exposure"); time.sleep(0.8)
                self.s.c("set_camera_bin", [1])
                self.s.c("set_subframe", [full])
                got = self.s.c("get_subframe")
                if isinstance(got, dict) and got.get("width") == CHIP[0]:
                    return
            except Exception:
                pass
        print("WARNING: could not restore the full-frame subframe; do it by hand", flush=True)


class GuideFrames:
    """Guide sensor over 4500. Independent of whatever the main camera does."""

    def __init__(self, host, exp_ms, gain, settle=0.15):
        from guide_image import GuideImage, start_looping
        self.settle = settle
        self.exp = exp_ms / 1000.0
        a = Air(host, 4400, timeout=10)
        try:
            a.call("stop_capture", [], timeout=10)
            time.sleep(0.3)
            print("guide exposure:", a.call("set_exposure", [int(exp_ms)], timeout=10).get("result"),
                  " gain:", a.call("set_gain", [int(gain)], timeout=10).get("result"), flush=True)
        finally:
            a.close()
        start_looping(host)
        self.g = GuideImage(host)
        self.g.begin_streaming()
        self.it = self.g.frames()

    def get(self):
        hdr, files = next(self.it)
        raw = next(iter(files.values()))
        w, h = hdr["width"], hdr["height"]
        if len(raw) == w * h:
            img = np.frombuffer(raw, dtype=np.uint8).reshape(h, w)
        else:
            img = np.frombuffer(raw, dtype=np.uint16).reshape(h, w)
        return img, w, h

    def after_move(self):
        # the frame in flight was exposed during the move: drop one
        time.sleep(self.settle)
        self.get()
        return self.get()

    def close(self):
        self.g.close()


# ---------------------------------------------------------------- mount
class Nudger:
    """Timed joystick pulses on 4400, through the Session's mount handle."""

    def __init__(self, s, rate_index):
        self.s = s
        st = s.mount().state()
        self.saved_rate = st.get("slew_rate_index")
        rates = st.get("slew_rate_list") or []
        self._call("scope_set_slew_rate", [int(rate_index)])
        print("slew rate index %s -> %s (%s)" % (self.saved_rate, rate_index,
              rates[rate_index] if rate_index < len(rates) else "?"), flush=True)

    def _call(self, m, p=None):
        return self.s.mount()._r(m, p or [])

    def pulse(self, direction, seconds):
        if seconds <= 0:
            return 0.0
        t0 = time.time()
        try:
            self._call("scope_move", [direction])
            time.sleep(seconds)
        finally:
            self._call("scope_move", ["none"])
        return time.time() - t0

    def stop(self):
        try:
            self._call("scope_move", ["none"])
        except Exception:
            pass

    def restore(self):
        self.stop()
        if self.saved_rate is not None:
            try:
                self._call("scope_set_slew_rate", [int(self.saved_rate)])
            except Exception:
                pass


# ---------------------------------------------------------------- calibration
def calibrate(fr, nd, pulse_s, scale_arcsec):
    """One pulse east, one north. Returns {axis: (vx, vy) px per second}."""
    cal, backlash, last = {}, {}, {}
    img, w, h = fr.get()
    p0, info = find_planet(img)
    if p0 is None:
        raise RuntimeError("calibration: planet not found: " + info)
    print("cal: start at (%.1f,%.1f)  %s" % (p0[0], p0[1], info), flush=True)
    for axis, (pos, neg) in DIRS.items():
        # Probe first with a short pulse to learn which way this axis moves the
        # image, then calibrate in the direction that heads for the frame
        # centre. 2026-09-02: a 2.5 s north take-up pushed Saturn out of the
        # top of a 512 px window because it happened to sit 47 px from it.
        probe = min(0.5, pulse_s / 3)
        nd.pulse(pos, probe)
        img, w, h = fr.after_move()
        pp, info = find_planet(img)
        if pp is None:
            nd.pulse(neg, probe)
            raise RuntimeError("calibration: planet lost after %s probe: %s" % (pos, info))
        to_centre = (w / 2 - pp[0], h / 2 - pp[1])
        moved = (pp[0] - p0[0], pp[1] - p0[1])
        flipped = (moved[0] * to_centre[0] + moved[1] * to_centre[1]) < 0 and math.hypot(*moved) > 2
        if flipped:
            pos, neg = neg, pos
            print("cal: %s probe moved away from centre (%+.0f,%+.0f px) -> calibrating with %s instead" % (
                axis, moved[0], moved[1], pos), flush=True)
        p0 = pp
        # Backlash: measured 2026-09-02 on the AM5N, the first ~1 s of a Dec
        # pulse after a reversal moves nothing (7 px/s vs 83 px/s loaded). So
        # pulse once to load the gear, re-measure the start, THEN measure.
        nd.pulse(pos, pulse_s)
        img, w, h = fr.after_move()
        pa, info = find_planet(img)
        if pa is None:
            nd.pulse(neg, pulse_s)
            raise RuntimeError("calibration: planet lost after %s take-up pulse: %s" % (pos, info))
        takeup = math.hypot(pa[0] - p0[0], pa[1] - p0[1])
        nd.pulse(pos, pulse_s)
        img, w, h = fr.after_move()
        p1, info = find_planet(img)
        if p1 is None:
            nd.pulse(neg, 2 * pulse_s)
            raise RuntimeError("calibration: planet lost after %s pulse: %s" % (pos, info))
        vec = ((p1[0] - pa[0]) / pulse_s, (p1[1] - pa[1]) / pulse_s)
        mag = math.hypot(*vec)
        print("cal: %-5s take-up %.2fs moved %.1f px; measured %.2fs moved %+.1f,%+.1f px = %.1f px/s (%.1f\"/s)  [%s]" % (
            pos, pulse_s, takeup, pulse_s, p1[0] - pa[0], p1[1] - pa[1], mag, mag * scale_arcsec, info), flush=True)
        if mag * pulse_s < 3:
            raise RuntimeError("calibration: %s pulse moved <3 px -- rate too low or axis stuck" % pos)
        # cal[axis] always describes DIRS[axis][0]; negate if we calibrated the other way
        cal[axis] = vec if not flipped else (-vec[0], -vec[1])
        backlash[axis] = max(0.0, pulse_s - takeup / mag) if mag > 0 else 0.0
        # go back: the reversal eats the backlash again, so pulse the dead time plus the travel
        nd.pulse(neg, 2 * pulse_s + backlash[axis])
        last[axis] = neg
        img, w, h = fr.after_move()
        p0b, _ = find_planet(img)
        if p0b is not None:
            print("cal: %-5s return leaves %.1f px residual; %s backlash ~%.2fs of pulse" % (
                neg, math.hypot(p0b[0] - p0[0], p0b[1] - p0[1]), axis, backlash[axis]), flush=True)
            p0 = p0b
    a, b = cal["ra"], cal["dec"]
    cosang = (a[0] * b[0] + a[1] * b[1]) / (math.hypot(*a) * math.hypot(*b))
    print("cal: RA/Dec axes %.0f deg apart" % math.degrees(math.acos(max(-1, min(1, cosang)))), flush=True)
    return cal, backlash, last


def solve_pulses(err, cal):
    a, b = cal["ra"], cal["dec"]
    det = a[0] * b[1] - a[1] * b[0]
    if abs(det) < 1e-6:
        raise RuntimeError("degenerate calibration")
    return {"ra": (err[0] * b[1] - err[1] * b[0]) / det,
            "dec": (a[0] * err[1] - a[1] * err[0]) / det}


# ---------------------------------------------------------------- where
def where(a):
    s = Session(a.host, a.key, with_mount=False)
    try:
        mf = MainFrames(s, "preview", 0.05, 250, binning=2)
        img, w, h = mf.get()
        p, info = find_planet(img)
        print("main (preview bin2 %dx%d): %s  %s -> full-res (%s)" % (
            w, h, p and "(%.1f,%.1f)" % p, info, p and "%.0f,%.0f" % (p[0] * 2, p[1] * 2)))
        s.c("stop_exposure")
    finally:
        s.close()
    gf = GuideFrames(a.host, a.guide_exp_ms, a.guide_gain)
    try:
        gf.get()
        img, w, h = gf.get()
        p, info = find_planet(img)
        print("guide (%dx%d): %s  %s" % (w, h, p and "(%.1f,%.1f)" % p, info))
    finally:
        gf.close()


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--key", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "embedded_key.pem"))
    ap.add_argument("--source", default="main", choices=["main", "guide"])
    ap.add_argument("--page", default="focus", help="main-camera page when not recording (focus|preview)")
    ap.add_argument("--exp", type=float, default=0.02, help="main camera seconds")
    ap.add_argument("--gain", type=int, default=100)
    ap.add_argument("--bin", type=int, default=1)
    ap.add_argument("--guide-exp-ms", type=int, default=50)
    ap.add_argument("--guide-gain", type=int, default=30)
    ap.add_argument("--video-page", default="rtmp", help="the Air's video tab is tagged rtmp (from the app: PAGE_VIDEO_TAG)")
    ap.add_argument("--video-roi", default=None, help="WxH sensor px, centred; switches the main camera to ROI video mode")
    ap.add_argument("--record", type=float, default=0, help="seconds of AVI to record on the Air while tracking")
    ap.add_argument("--rate", type=int, default=0, help="slew_rate_list index for pulses (0=1x 1=2x 2=4x 3=8x); 1x holds, 4x hunts")
    ap.add_argument("--cal-pulse", type=float, default=2.5, help="s; ~90 px at 1x full-res")
    ap.add_argument("--calibrate-only", action="store_true")
    ap.add_argument("--target", default=None, help="x,y pixel to hold the planet at (default frame centre)")
    ap.add_argument("--deadband", type=float, default=12.0, help="px; must exceed min-pulse travel + backlash allowance")
    ap.add_argument("--gain-loop", type=float, default=0.6)
    ap.add_argument("--max-pulse", type=float, default=1.5)
    ap.add_argument("--min-pulse", type=float, default=0.12)
    ap.add_argument("--settle", type=float, default=0.15)
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--scale", type=float, default=0.473, help="arcsec/px of the SOURCE frames, reporting only")
    ap.add_argument("--log", default=None)
    ap.add_argument("--where", action="store_true", help="print the planet position on both sensors and exit")
    a = ap.parse_args()
    if a.where:
        return where(a)

    s = Session(a.host, a.key, with_mount=True)
    nd = fr = None
    recording = False
    logf = open(a.log, "a") if a.log else None
    try:
        st = s.mount().state()
        if not st.get("is_enable_track"):
            print("tracking was OFF -- enabling", flush=True)
            s.mount().set_tracking(True)

        roi = tuple(int(x) for x in a.video_roi.lower().split("x")) if a.video_roi else None
        if a.source == "guide":
            if roi or a.record:
                # put the main camera into ROI video mode; the guide stream is separate
                MainFrames(s, a.video_page if roi else None, a.exp, a.gain, roi=roi)
            fr = GuideFrames(a.host, a.guide_exp_ms, a.guide_gain, settle=a.settle)
            fr.get()
        else:
            fr = MainFrames(s, a.video_page if roi else a.page, a.exp, a.gain, binning=a.bin, roi=roi, settle=a.settle)
        img, w, h = fr.get()
        p, info = find_planet(img)
        print("source %s frame %dx%d; planet %s  %s" % (a.source, w, h, p and "(%.1f,%.1f)" % p, info), flush=True)
        if p is None:
            print("planet not in frame -- get it in the field first"); return 1
        target = tuple(float(t) for t in a.target.split(",")) if a.target else (w / 2.0, h / 2.0)

        nd = Nudger(s, a.rate)
        cal, backlash, last_dir = calibrate(fr, nd, a.cal_pulse, a.scale)
        if a.calibrate_only:
            return 0

        if a.record:
            r = s.c("start_record_avi")
            recording = (r == 0)
            print("start_record_avi -> %s%s" % (r, "" if recording else "  (NOT recording)"), flush=True)
            a.seconds = max(a.seconds, a.record)

        print("tracking to (%.0f,%.0f) for %.0fs: deadband %.0f px, loop gain %.2f, pulses %.2f-%.2fs at rate %d" % (
            target[0], target[1], a.seconds, a.deadband, a.gain_loop, a.min_pulse, a.max_pulse, a.rate), flush=True)
        print("   t     planet x,y     err px   err\"    pulses", flush=True)
        t0 = time.time(); n = 0; lost = 0; last_ev = ""
        while time.time() - t0 < a.seconds:
            tc = time.time()
            img, w, h = fr.get()
            p, info = find_planet(img)
            if recording and a.source == "main":
                s.c("get_camera_state")
            for e in s.air.drain_events():
                if e.get("Event") == "AviRecord":
                    last_ev = "rec %ss %sfps" % (e.get("lapse_sec"), e.get("write_file_fps") or e.get("fps"))
            if p is None:
                lost += 1
                print("%6.1f  planet lost (%s) x%d" % (tc - t0, info, lost), flush=True)
                if lost >= 8:
                    print("lost 8 frames running -- stopping"); break
                continue
            lost = 0
            err = (target[0] - p[0], target[1] - p[1])
            e_px = math.hypot(*err)
            pulses = ""
            if e_px > a.deadband:
                t = solve_pulses((err[0] * a.gain_loop, err[1] * a.gain_loop), cal)
                for axis in ("ra", "dec"):
                    sec = t[axis]
                    d = DIRS[axis][0] if sec > 0 else DIRS[axis][1]
                    sec = min(abs(sec), a.max_pulse)
                    if sec < a.min_pulse / 2:
                        continue
                    sec = max(sec, a.min_pulse)
                    extra = backlash.get(axis, 0.0) if d != last_dir[axis] else 0.0
                    nd.pulse(d, sec + extra)
                    last_dir[axis] = d
                    pulses += "%s %.2fs%s  " % (d, sec, ("+%.2f" % extra) if extra else "")
                if pulses:
                    time.sleep(a.settle)
            n += 1
            print("%6.1f  %7.1f,%7.1f  %7.1f  %6.1f   %s %s" % (tc - t0, p[0], p[1], e_px, e_px * a.scale, pulses or "-", last_ev), flush=True)
            if logf:
                logf.write("%.3f,%.2f,%.2f,%.2f,%s\n" % (tc, p[0], p[1], e_px, pulses.strip())); logf.flush()
        el = time.time() - t0
        print("done: %d cycles in %.0fs = %.2f Hz" % (n, el, n / el if el else 0))
    finally:
        if nd:
            nd.restore()
        if recording:
            try:
                print("stop_record_avi ->", s.c("stop_record_avi"))
            except Exception:
                pass
        try:
            s.c("stop_exposure")
        except Exception:
            pass
        if fr:
            try:
                fr.close()
            except Exception:
                pass
        s.close()
        if logf:
            logf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
