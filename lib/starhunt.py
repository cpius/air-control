#!/usr/bin/env python3
"""Joystick-driven star search — for when plate solving is not available.

Plate solving needs focus. Before focus you cannot solve, and if the mount has
been moved or is otherwise unreferenced you cannot trust an absolute goto
either. Directional joystick moves are RELATIVE, so they work regardless of the
pointing model — which makes them the right tool for finding a bright star to
focus on in the first place.

Three things make this work in practice:

  * SELF-CALIBRATED STEPS. The slew-rate index is not a reliable predictor of
    on-sky speed (the same index measured ~7.5x faster at Dec 90 than at Dec 45,
    each run internally consistent). So we measure the real displacement of a
    test nudge with scope_get_info and size every step from that.

  * A DEFOCUS-TOLERANT DETECTOR. Out of focus a bright star is a large, low
    contrast disc — invisible to a point-source finder. We block-downsample at
    two scales, remove a fitted quadratic (vignetting plus sky gradient), and
    look for the strongest excursion. A quadratic leaves blobs up to roughly a
    third of the frame intact, unlike a local-median detrend which eats them.

  * DYNAMIC HOT-PIXEL REJECTION. Hot pixels are the brightest things in a short
    exposure and will happily masquerade as the star you are hunting. They are
    trivially separable: they do not move when the mount does. We nudge once at
    startup and blacklist whatever stayed put.

Frames come from the native MainImageSocket (port 4800) rather than Alpaca, so
they are free of the Alpaca driver's leading-quarter scaling artifact.

    python3 starhunt.py --host <air-ip> --key embedded_key.pem --rings 2
"""

import argparse
import array
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from air_rpc import Air
from airlog import add_log_args, configure_logging, get_logger
from joystick import Joystick
from mount import Mount
from main_image import MainImage

log = get_logger("starhunt")

MAIN_PORT = 4700


# --------------------------------------------------------------------------
# frame acquisition (native 4700 expose + 4800 download)
# --------------------------------------------------------------------------

class Camera:
    """Expose on 4700, download the frame from 4800."""

    def __init__(self, host, key, cam_name=None):
        self.host, self.key = host, key
        self.n_frames = 0
        with log.slow("bringing the camera up") as tk:
            tk.set("4700 handshake")
            self.air = Air(host, MAIN_PORT, key=key)
            if not self.air.verified:
                raise RuntimeError("4700 handshake failed — check --key")
            tk.set("connecting 4800")
            self.img = MainImage(host)
            tk.set("opening the sensor")
            self._open(cam_name)

    def _call(self, m, p=None, t=20, tries=3):
        """The Air drops long-lived 4700 connections; re-handshake and retry.

        Verification is per-connection, so a reconnect must redo it — but the
        camera stays open server-side, so it does not need reopening.
        """
        for i in range(tries):
            try:
                r = self.air.call(m, p or [], timeout=t)
                return r.get("result", r.get("error"))
            except Exception as e:
                if i == tries - 1:
                    log.error("%s failed %d times: %s", m, tries, e)
                    raise
                log.warn("%s failed (%s) — re-handshaking 4700, attempt %d/%d",
                         m, e, i + 2, tries)
                try:
                    self.air.close()
                except Exception:
                    pass
                time.sleep(1.0)
                self.air = Air(self.host, MAIN_PORT, key=self.key)

    def _open(self, cam_name):
        # A bare open_camera opens the GUIDE sensor; open the main one by name.
        cams = self._call("get_connected_cameras") or []
        if cam_name is None:
            best = max(cams, key=lambda c: c.get("chip_size", [0, 0])[0], default=None)
            if not best:
                raise RuntimeError("no cameras reported by the Air")
            cam_name = best["name"]
        log.info("opening %r (a bare open_camera would grab the GUIDE sensor)",
                 cam_name)
        self._call("close_camera")
        time.sleep(1.5)
        self._call("open_camera", [cam_name], t=25)
        time.sleep(1.5)
        st = self._call("get_camera_state")
        if not (isinstance(st, dict) and st.get("name") == cam_name):
            raise RuntimeError(f"could not open {cam_name!r} (state={st}); "
                               "is an Alpaca client holding the sensor?")
        self.name = cam_name
        info = self._call("get_camera_info") or {}
        self.pixel_um = info.get("pixel_size_um", 0) or 0
        self.chip = info.get("chip_size", [0, 0])
        log.info("camera %s open: chip=%s pixel=%sum", cam_name, self.chip,
                 self.pixel_um)
        print(f"camera: {cam_name}  chip={self.chip}  pixel={self.pixel_um}um")

    def focal_length(self):
        return self._call("get_focal_length") or 0

    def grab(self, exp_s, gain):
        """Expose and download. Anything over ~10s needs the keepalive below.

        The Air drops an idle 4700 socket after ~15s. A client that fires
        start_exposure and then waits in silence gets disconnected before the
        'complete' event arrives, so long exposures fail with "exposure did not
        complete" while the camera is in fact perfectly happy. Poking a cheap
        read every few seconds keeps the connection up. Note we poke through
        self.air directly rather than self._call: _call's reconnect would swap
        the socket and take the queued events with it.
        """
        log.debug("expose %.2fs @ gain %d", exp_s, gain)
        self._call("set_control_value", ["Exposure", int(exp_s * 1_000_000)])
        self._call("set_control_value", ["Gain", int(gain)])
        self.air.drain_events()
        self._call("start_exposure")
        t0 = last_poke = time.time()
        done = False
        pokes = [0]
        # The exposure itself is `exp_s`, then the Air needs time to read out.
        # Count both down: an exposure that never completes is common enough
        # (dropped socket, camera held elsewhere) to be worth watching live.
        with log.slow("exposing %.2fs" % exp_s, quiet_for=1.0,
                      detail=lambda: "%.1fs of %.1fs%s, %d keepalive(s)"
                                     % (time.time() - t0, exp_s,
                                        " — now reading out"
                                        if time.time() - t0 > exp_s else "",
                                        pokes[0])):
            while time.time() - t0 < exp_s + 40:
                for e in self.air.drain_events():
                    if e.get("Event") == "Exposure" and e.get("state") == "complete":
                        done = True
                if done:
                    break
                if time.time() - last_poke > 4:
                    # The Air drops an idle 4700 socket after ~15s.
                    try:
                        self.air.call("get_camera_state", [], timeout=8)
                    except Exception as e:
                        log.debug("keepalive poke failed: %s", e)
                    pokes[0] += 1
                    last_poke = time.time()
                time.sleep(0.2)
        if not done:
            log.error("no Exposure/complete event within %.0fs", exp_s + 40)
            raise RuntimeError("exposure did not complete")
        log.debug("exposure complete in %.2fs; downloading", time.time() - t0)
        try:
            hdr, files = self.img.get_image("get_current_img", 0)
        except Exception as e:
            log.warn("4800 download failed (%s) — reconnecting the image socket", e)
            self.img.close()
            self.img = MainImage(self.host)
            hdr, files = self.img.get_image("get_current_img", 0)
        raw = next(iter(files.values()))
        self.n_frames += 1
        log.info("frame %d: %dx%d in %.1fs total", self.n_frames, hdr["width"],
                 hdr["height"], time.time() - t0)
        a = array.array("H")
        a.frombytes(raw)
        if sys.byteorder == "big" and not hdr["isBigEndian"]:
            a.byteswap()
        return a, hdr["width"], hdr["height"], hdr

    def close(self):
        log.info("closing the camera after %d frame(s)", self.n_frames)
        try:
            self.img.close()
        finally:
            self.air.close()


# --------------------------------------------------------------------------
# detection (frames are ROW-major: index = y*w + x)
# --------------------------------------------------------------------------

def downsample(v, w, h, B):
    """Block-average the frame by B. Pure Python over every pixel.

    On the ASI585's 3840x2160 this touches 8.3 million values, which is tens of
    seconds of CPU here -- the single slowest non-network step in the toolkit
    and, before this, completely silent. It reports its row progress every
    second so a slow detect is distinguishable from a hung one.
    """
    dw, dh = w // B, h // B
    out = [0.0] * (dw * dh)
    gy_done = [0]
    t0 = time.time()
    with log.slow("downsample %dx%d by %d" % (w, h, B), quiet_for=1.0,
                  detail=lambda: "%d/%d rows (%.0f%%, %.0fk px/s)"
                                 % (gy_done[0], dh, 100.0 * gy_done[0] / max(dh, 1),
                                    gy_done[0] * B * w / max(time.time() - t0, 1e-3) / 1000.0)):
        for gy in range(dh):
            base = gy * B
            for yy in range(base, base + B):
                row = yy * w
                for gx in range(dw):
                    s = 0
                    off = row + gx * B
                    for xx in range(off, off + B):
                        s += v[xx]
                    out[gy * dw + gx] += s
            gy_done[0] += 1
    inv = 1.0 / (B * B)
    log.debug("downsample by %d -> %dx%d in %.1fs", B, dw, dh, time.time() - t0)
    return [x * inv for x in out], dw, dh


def robust(vals):
    s = sorted(vals)
    n = len(s)
    med = s[n // 2]
    q1, q3 = s[n // 4], s[3 * n // 4]
    return med, max((q3 - q1) / 1.349, 1e-6)


def quad_detrend(dv, dw, dh):
    """Least-squares remove a + bx + cy + dx^2 + ey^2 + fxy."""
    t0 = time.time()
    basis = []
    for y in range(dh):
        fy = (y / dh) - 0.5
        for x in range(dw):
            fx = (x / dw) - 0.5
            basis.append((1.0, fx, fy, fx * fx, fy * fy, fx * fy))
    A = [[0.0] * 6 for _ in range(6)]
    b = [0.0] * 6
    for i, bf in enumerate(basis):
        v = dv[i]
        for r in range(6):
            b[r] += bf[r] * v
            for c in range(6):
                A[r][c] += bf[r] * bf[c]
    M = [A[r][:] + [b[r]] for r in range(6)]
    for col in range(6):
        piv = max(range(col, 6), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        if abs(M[col][col]) < 1e-12:
            continue
        for r in range(6):
            if r != col:
                f = M[r][col] / M[col][col]
                for c in range(col, 7):
                    M[r][c] -= f * M[col][c]
    coef = [M[r][6] / M[r][r] if abs(M[r][r]) > 1e-12 else 0.0 for r in range(6)]
    log.debug("quad detrend over %dx%d in %.2fs; coef=%s", dw, dh,
              time.time() - t0, ["%.1f" % c for c in coef])
    return [dv[i] - sum(coef[k] * bf[k] for k in range(6)) for i, bf in enumerate(basis)]


def blobs(v, w, h, scales=(8, 32), top=6):
    """Strongest excursions, in full-frame pixel coords, as (sigma, x, y)."""
    found = []
    t0 = time.time()
    scale_done = [0]
    with log.slow("detect blobs at scales %s" % (scales,), quiet_for=1.0,
                  detail=lambda: "scale %d/%d, %d found so far"
                                 % (scale_done[0], len(scales), len(found))):
        for B in scales:
            dv, dw, dh = downsample(v, w, h, B)
            flat = quad_detrend(dv, dw, dh)
            med, sig = robust(flat)
            log.debug("scale %d: median %.1f sigma %.2f", B, med, sig)
            order = sorted(range(len(flat)), key=lambda i: -flat[i])[:40]
            picked = []
            for i in order:
                x, y = (i % dw + 0.5) * B, (i // dw + 0.5) * B
                if any(abs(x - px) < 3 * B and abs(y - py) < 3 * B
                       for _, px, py in picked):
                    continue
                picked.append(((flat[i] - med) / sig, x, y))
                if len(picked) >= top:
                    break
            found.extend(picked)
            scale_done[0] += 1
    found.sort(reverse=True)
    log.info("blob detection done in %.1fs: %d candidate(s)%s",
             time.time() - t0, len(found),
             ", strongest %.1f sigma at (%.0f,%.0f)" % found[0] if found else "")
    return found


def saturated_fraction(v):
    sample = v[::97]
    return sum(1 for x in sample if x >= 64000) / max(len(sample), 1)


# --------------------------------------------------------------------------

def spiral(nring):
    yield (0, 0)
    for r in range(1, nring + 1):
        for i in range(-r, r):     yield (i, -r)
        for i in range(-r, r):     yield (r, i)
        for i in range(r, -r, -1): yield (i, r)
        for i in range(r, -r, -1): yield (-r, i)


def find_fixed_sources(cam, j, exp, gain, nudge_s, axis="east", tol=12.0):
    """Blacklist anything that does NOT move when the mount does — i.e. hot
    pixels and sensor defects, which otherwise dominate short exposures."""
    # Two full frames plus two nudges: the better part of a minute, all of it
    # before the search proper starts.
    with log.slow("identifying fixed sources (hot pixels)") as tk:
        tk.set("frame 1 of 2")
        a, w, h, _ = cam.grab(exp, gain)
        before = blobs(a, w, h)
        back = {"east": "west", "north": "south"}[axis]
        tk.set("nudging %s %.1fs" % (axis, nudge_s))
        j.nudge(axis, nudge_s)
        tk.set("frame 2 of 2")
        b, w, h, _ = cam.grab(exp, gain)
        after = blobs(b, w, h)
        tk.set("nudging back %s" % back)
        j.nudge(back, nudge_s)
        fixed = []
        for _, x, y in before:
            if any(abs(x - x2) < tol and abs(y - y2) < tol for _, x2, y2 in after):
                fixed.append((x, y))
    log.info("%d of %d source(s) did not move with the mount — blacklisted",
             len(fixed), len(before))
    for x, y in fixed:
        log.debug("  fixed source at (%.0f, %.0f)", x, y)
    return fixed


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--key", default="embedded_key.pem",
                    help="RSA key for the 4700 handshake")
    ap.add_argument("--camera", help="main camera name (default: largest chip)")
    ap.add_argument("--rings", type=int, default=2, help="spiral rings (2 -> 25 cells)")
    ap.add_argument("--step-frac", type=float, default=0.7,
                    help="step as a fraction of the SHORT field axis")
    ap.add_argument("--rate", type=int, default=3, help="slew_rate_list index")
    ap.add_argument("--exp", type=float, default=2.0)
    ap.add_argument("--gain", type=int, default=200)
    ap.add_argument("--trigger", type=float, default=12.0, help="sigma to flag a candidate")
    ap.add_argument("--calib-secs", type=float, default=2.0)
    ap.add_argument("--lat", type=float,
                    help="site latitude, restored after attaching the mount")
    ap.add_argument("--lon", type=float, help="site longitude")
    add_log_args(ap)
    a = ap.parse_args()
    configure_logging(a)
    log.info("starhunt: %d ring(s), exp %.1fs gain %d, rate %d, trigger %.0f sigma",
             a.rings, a.exp, a.gain, a.rate, a.trigger)

    # The Air drops the mount on restart and nothing else re-attaches it.
    # Attach it first, restoring the site — attaching zeroes it to [0, 0],
    # which would silently corrupt every alt/az this search reports.
    site = (a.lat, a.lon) if a.lat is not None and a.lon is not None else None
    mnt = Mount(a.host)
    try:
        if mnt.ensure_connected(restore=site):
            print("attached the mount")
        ok, lat, lon = mnt.check_location()
        if not ok:
            print(f"  WARNING: site location is [{lat}, {lon}] — alt/az will be "
                  "wrong. Pass --lat/--lon, or set it with mount.py location")
    finally:
        mnt.close()

    j = Joystick(a.host)
    cam = None
    try:
        cam = Camera(a.host, a.key, a.camera)
        fl = cam.focal_length()
        if not (fl and cam.pixel_um and cam.chip[0]):
            raise RuntimeError("could not read focal length / pixel size from the Air")
        fov_x = math.degrees(2 * math.atan(cam.chip[0] * cam.pixel_um * 1e-3 / 2 / fl)) * 60
        fov_y = math.degrees(2 * math.atan(cam.chip[1] * cam.pixel_um * 1e-3 / 2 / fl)) * 60
        print(f"focal length {fl:.0f}mm -> field {fov_x:.1f}' x {fov_y:.1f}'")

        st = j.state()
        print(f"start RA={st['RA']:.4f} Dec={st['Dec']:.3f} alt={st['Alt']:.1f}")

        print("calibrating slew rate here (the index alone is not trustworthy) ...")
        # Four timed nudges plus settling: ~15s of the mount actually moving.
        with log.slow("calibrating the slew rate in situ") as tk:
            j.set_rate(a.rate)
            time.sleep(0.4)
            tk.set("north %.1fs" % a.calib_secs)
            dra, ddec, _, _ = j.nudge("north", a.calib_secs)
            ns = math.hypot(dra, ddec) * 60 / a.calib_secs
            tk.set("south %.1fs (returning)" % a.calib_secs)
            j.nudge("south", a.calib_secs)
            tk.set("east %.1fs" % a.calib_secs)
            dra, ddec, _, _ = j.nudge("east", a.calib_secs)
            ew = math.hypot(dra, ddec) * 60 / a.calib_secs
            tk.set("west %.1fs (returning)" % a.calib_secs)
            j.nudge("west", a.calib_secs)
        if ns <= 0:
            log.error("calibration measured no N/S motion at all")
            raise RuntimeError("calibration measured no motion — is the mount connected "
                               "and unparked?")
        step = a.step_frac * fov_y
        t_ns = step / ns
        # On-sky RA motion is cos(Dec)-compressed, so near the pole an E/W step
        # takes arbitrarily long — and at the pole it is meaningless. Search in
        # Dec only there rather than emitting a divide-by-nothing step time.
        if ew <= 0 or step / ew > 60:
            print(f"  E/W motion is negligible at Dec {st['Dec']:.1f} "
                  f"(cos-compressed) — searching in Dec only")
            t_ew = None
        else:
            t_ew = step / ew
        print(f"  N/S {ns:.2f}'/s   E/W {ew:.2f}'/s   step {step:.1f}' -> "
              f"{t_ns:.2f}s N/S, " + (f"{t_ew:.2f}s E/W" if t_ew else "E/W disabled"))
        log.info("measured N/S %.2f'/s, E/W %.2f'/s; step %.1f' = %.2fs N/S, %s",
                 ns, ew, step, t_ns, "%.2fs E/W" % t_ew if t_ew else "E/W disabled")
        if t_ns > 30 or (t_ew and t_ew > 30):
            log.warn("steps are very long (%.0fs) — use a faster --rate", t_ns)
            print("  WARNING: steps are very long — use a faster --rate")

        print("identifying fixed sources (hot pixels do not move with the mount) ...")
        fixed = find_fixed_sources(cam, j, a.exp, a.gain,
                                   min(t_ew or t_ns, 4.0),
                                   axis="east" if t_ew else "north")
        print(f"  blacklisted {len(fixed)} fixed source(s)")

        prev, hits = (0, 0), []
        cells = list(spiral(a.rings))
        # Each cell is a slew plus an exposure plus a detect: 30-60s apiece, so
        # the whole search is minutes to tens of minutes. Report where in it we
        # are once a second, and estimate what is left.
        t_search = time.time()
        cell_done = [0]
        log.info("searching %d cell(s); ~%.0fs per cell -> roughly %.0f min",
                 len(cells), t_ns + a.exp + 20,
                 len(cells) * (t_ns + a.exp + 20) / 60.0)
        with log.slow("spiral search", quiet_for=1.0,
                      detail=lambda: "cell %d/%d, %d candidate(s), %.0fs elapsed, "
                                     "~%.0fs left"
                                     % (cell_done[0], len(cells), len(hits),
                                        time.time() - t_search,
                                        (len(cells) - cell_done[0])
                                        * (time.time() - t_search)
                                        / max(cell_done[0], 1))) as tk:
            for k, (gx, gy) in enumerate(cells):
                tk.set("cell(%+d,%+d)" % (gx, gy))
                log.info("--- cell %d/%d (%+d,%+d) ---", k + 1, len(cells), gx, gy)
                dx, dy = gx - prev[0], gy - prev[1]
                if t_ew is None:
                    dx = 0                      # E/W disabled near the pole
                for _ in range(abs(dx)):
                    j.nudge("east" if dx > 0 else "west", t_ew)
                for _ in range(abs(dy)):
                    j.nudge("north" if dy > 0 else "south", t_ns)
                prev = (gx, gy)
                cell_done[0] = k + 1

                v, w, h, _ = cam.grab(a.exp, a.gain)
                s = j.state()
                sat = saturated_fraction(v)
                if sat > 0.5:
                    log.warn("cell (%+d,%+d) is %.0f%% saturated — shorten --exp",
                             gx, gy, sat * 100)
                    print(f"  [{k:3d}] cell({gx:+d},{gy:+d}) skipped: {sat*100:.0f}% saturated "
                          f"— shorten --exp", flush=True)
                    continue
                cand = [b for b in blobs(v, w, h)
                        if not any(abs(b[1] - fx) < 12 and abs(b[2] - fy) < 12
                                   for fx, fy in fixed)]
                if not cand:
                    log.info("cell (%+d,%+d): nothing above the fixed pattern", gx, gy)
                    print(f"  [{k:3d}] cell({gx:+d},{gy:+d}) nothing above the fixed pattern",
                          flush=True)
                    continue
                sig, x, y = cand[0]
                flag = "  <<< CANDIDATE" if sig >= a.trigger else ""
                print(f"  [{k:3d}] cell({gx:+d},{gy:+d}) RA={s['RA']:.4f} Dec={s['Dec']:+.3f}"
                      f"  peak={sig:6.1f}sigma at ({x:.0f},{y:.0f}){flag}", flush=True)
                if flag:
                    log.info("CANDIDATE in cell (%+d,%+d): %.1f sigma at (%.0f,%.0f) "
                             "RA=%.4f Dec=%+.3f", gx, gy, sig, x, y, s["RA"], s["Dec"])
                    hits.append((sig, gx, gy, s["RA"], s["Dec"]))
        log.info("spiral search done in %.0fs: %d candidate(s) over %d cell(s)",
                 time.time() - t_search, len(hits), len(cells))

        if hits:
            hits.sort(reverse=True)
            print("\ncandidates (strongest first):")
            for sig, gx, gy, ra, dec in hits:
                print(f"  {sig:6.1f}sigma  cell({gx:+d},{gy:+d})  RA={ra:.4f} Dec={dec:+.3f}")
        else:
            print("\nno candidates — widen with --rings, or raise --exp/--gain")
    finally:
        j.close()            # always stops motion
        if cam:
            cam.close()


if __name__ == "__main__":
    sys.exit(main())
