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
from joystick import Joystick
from mount import Mount
from main_image import MainImage

MAIN_PORT = 4700


# --------------------------------------------------------------------------
# frame acquisition (native 4700 expose + 4800 download)
# --------------------------------------------------------------------------

class Camera:
    """Expose on 4700, download the frame from 4800."""

    def __init__(self, host, key, cam_name=None):
        self.host, self.key = host, key
        self.air = Air(host, MAIN_PORT, key=key)
        if not self.air.verified:
            raise RuntimeError("4700 handshake failed — check --key")
        self.img = MainImage(host)
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
            except Exception:
                if i == tries - 1:
                    raise
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
        self._call("set_control_value", ["Exposure", int(exp_s * 1_000_000)])
        self._call("set_control_value", ["Gain", int(gain)])
        self.air.drain_events()
        self._call("start_exposure")
        t0 = last_poke = time.time()
        done = False
        while time.time() - t0 < exp_s + 40:
            for e in self.air.drain_events():
                if e.get("Event") == "Exposure" and e.get("state") == "complete":
                    done = True
            if done:
                break
            if time.time() - last_poke > 4:
                try:
                    self.air.call("get_camera_state", [], timeout=8)
                except Exception:
                    pass          # the download path below re-establishes if needed
                last_poke = time.time()
            time.sleep(0.2)
        if not done:
            raise RuntimeError("exposure did not complete")
        try:
            hdr, files = self.img.get_image("get_current_img", 0)
        except Exception:
            self.img.close()
            self.img = MainImage(self.host)
            hdr, files = self.img.get_image("get_current_img", 0)
        raw = next(iter(files.values()))
        a = array.array("H")
        a.frombytes(raw)
        if sys.byteorder == "big" and not hdr["isBigEndian"]:
            a.byteswap()
        return a, hdr["width"], hdr["height"], hdr

    def close(self):
        try:
            self.img.close()
        finally:
            self.air.close()


# --------------------------------------------------------------------------
# detection (frames are ROW-major: index = y*w + x)
# --------------------------------------------------------------------------

def downsample(v, w, h, B):
    dw, dh = w // B, h // B
    out = [0.0] * (dw * dh)
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
    inv = 1.0 / (B * B)
    return [x * inv for x in out], dw, dh


def robust(vals):
    s = sorted(vals)
    n = len(s)
    med = s[n // 2]
    q1, q3 = s[n // 4], s[3 * n // 4]
    return med, max((q3 - q1) / 1.349, 1e-6)


def quad_detrend(dv, dw, dh):
    """Least-squares remove a + bx + cy + dx^2 + ey^2 + fxy."""
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
    return [dv[i] - sum(coef[k] * bf[k] for k in range(6)) for i, bf in enumerate(basis)]


def blobs(v, w, h, scales=(8, 32), top=6):
    """Strongest excursions, in full-frame pixel coords, as (sigma, x, y)."""
    found = []
    for B in scales:
        dv, dw, dh = downsample(v, w, h, B)
        flat = quad_detrend(dv, dw, dh)
        med, sig = robust(flat)
        order = sorted(range(len(flat)), key=lambda i: -flat[i])[:40]
        picked = []
        for i in order:
            x, y = (i % dw + 0.5) * B, (i // dw + 0.5) * B
            if any(abs(x - px) < 3 * B and abs(y - py) < 3 * B for _, px, py in picked):
                continue
            picked.append(((flat[i] - med) / sig, x, y))
            if len(picked) >= top:
                break
        found.extend(picked)
    found.sort(reverse=True)
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
    a, w, h, _ = cam.grab(exp, gain)
    before = blobs(a, w, h)
    back = {"east": "west", "north": "south"}[axis]
    j.nudge(axis, nudge_s)
    b, w, h, _ = cam.grab(exp, gain)
    after = blobs(b, w, h)
    j.nudge(back, nudge_s)
    fixed = []
    for _, x, y in before:
        if any(abs(x - x2) < tol and abs(y - y2) < tol for _, x2, y2 in after):
            fixed.append((x, y))
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
    a = ap.parse_args()

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
        j.set_rate(a.rate)
        time.sleep(0.4)
        dra, ddec, _, _ = j.nudge("north", a.calib_secs)
        ns = math.hypot(dra, ddec) * 60 / a.calib_secs
        j.nudge("south", a.calib_secs)
        dra, ddec, _, _ = j.nudge("east", a.calib_secs)
        ew = math.hypot(dra, ddec) * 60 / a.calib_secs
        j.nudge("west", a.calib_secs)
        if ns <= 0:
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
        if t_ns > 30 or (t_ew and t_ew > 30):
            print("  WARNING: steps are very long — use a faster --rate")

        print("identifying fixed sources (hot pixels do not move with the mount) ...")
        fixed = find_fixed_sources(cam, j, a.exp, a.gain,
                                   min(t_ew or t_ns, 4.0),
                                   axis="east" if t_ew else "north")
        print(f"  blacklisted {len(fixed)} fixed source(s)")

        prev, hits = (0, 0), []
        for k, (gx, gy) in enumerate(spiral(a.rings)):
            dx, dy = gx - prev[0], gy - prev[1]
            if t_ew is None:
                dx = 0                      # E/W disabled near the pole
            for _ in range(abs(dx)):
                j.nudge("east" if dx > 0 else "west", t_ew)
            for _ in range(abs(dy)):
                j.nudge("north" if dy > 0 else "south", t_ns)
            prev = (gx, gy)

            v, w, h, _ = cam.grab(a.exp, a.gain)
            s = j.state()
            sat = saturated_fraction(v)
            if sat > 0.5:
                print(f"  [{k:3d}] cell({gx:+d},{gy:+d}) skipped: {sat*100:.0f}% saturated "
                      f"— shorten --exp", flush=True)
                continue
            cand = [b for b in blobs(v, w, h)
                    if not any(abs(b[1] - fx) < 12 and abs(b[2] - fy) < 12
                               for fx, fy in fixed)]
            if not cand:
                print(f"  [{k:3d}] cell({gx:+d},{gy:+d}) nothing above the fixed pattern",
                      flush=True)
                continue
            sig, x, y = cand[0]
            flag = "  <<< CANDIDATE" if sig >= a.trigger else ""
            print(f"  [{k:3d}] cell({gx:+d},{gy:+d}) RA={s['RA']:.4f} Dec={s['Dec']:+.3f}"
                  f"  peak={sig:6.1f}sigma at ({x:.0f},{y:.0f}){flag}", flush=True)
            if flag:
                hits.append((sig, gx, gy, s["RA"], s["Dec"]))

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
