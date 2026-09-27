#!/usr/bin/env python3
"""Horizon survey by goto + star count. Requires a calibrated pointing frame.

Precondition: plate solve and sync first, so the mount's reported Alt/Az IS true
Alt/Az. Then no offset model is needed and every reading is self-describing.

Classifier: count point sources. Open sky returns dozens; a wall returns the
fixed hot-pixel floor of ~6-7 and no more. Exposure lengthens at low altitude
because extinction there wipes out faint stars, which would otherwise make open
sky look blocked exactly where the roofline is.

Two hard-won rules baked in:
  * ONE process may touch port 4400. Check `lsof -i @<air>` before starting.
  * `move_status` is unreliable during a slew -- it reads "none" while the mount
    is still moving. Settling is detected by position going stable instead.
"""
import json, math, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import numpy as np
import cv2
from air_rpc import Air
from starhunt import Camera
from airlog import get_logger

log = get_logger("survey2")
H = "192.168.1.35"
KEY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "embedded_key.pem")
LAT = 55.689444
ALT_FLOOR = 4.0
D, R = math.degrees, math.radians


def altaz_to_radec(az, alt, lst):
    az_r, alt_r, lat = R(az), R(alt), R(LAT)
    dec = math.asin(math.sin(alt_r) * math.sin(lat) +
                    math.cos(alt_r) * math.cos(lat) * math.cos(az_r))
    cosha = (math.sin(alt_r) - math.sin(dec) * math.sin(lat)) / (math.cos(dec) * math.cos(lat))
    ha = math.acos(max(-1.0, min(1.0, cosha)))
    if math.sin(az_r) > 0:
        ha = -ha
    return (lst - D(ha) / 15.0) % 24, D(dec)


class Survey2:
    def __init__(self, out):
        self.out = out
        self.m = Air(H, 4400, timeout=8)
        self.cam = Camera(H, KEY, cam_name="ZWO ASI585MC Air")
        self.cam._call("set_camera_bin", [2], t=10)

    def _m(self, method, params=None, t=8, n=4):
        for i in range(n):
            try:
                return self.m.call(method, params or [], timeout=t)
            except Exception:
                try: self.m.close()
                except Exception: pass
                time.sleep(1.0)
                try: self.m = Air(H, 4400, timeout=8)
                except Exception: time.sleep(2.0)
        raise RuntimeError(method + " failed")

    def state(self):
        return self._m("scope_get_info")["result"]

    def goto(self, ra, dec, timeout=70):
        """Slew and wait for the position to stop changing.

        `move_status` reads "none" mid-slew on this firmware, so it cannot be
        used to detect arrival. Position stability can.
        """
        self._m("scope_goto", [ra, dec], t=12)
        t0 = time.time()
        prev = None
        stable = 0
        while time.time() - t0 < timeout:
            time.sleep(1.5)
            s = self.state()
            if prev is not None:
                dra = abs((s["RA"] - prev[0]) * 15)
                if dra > 180: dra = 360 - dra
                if dra < 0.02 and abs(s["Dec"] - prev[1]) < 0.02:
                    stable += 1
                    if stable >= 2:
                        return s
                else:
                    stable = 0
            prev = (s["RA"], s["Dec"])
        return self.state()

    def recover_camera(self):
        log.warn("camera wedged — close/open cycle")
        for m, p, t in (("stop_exposure", None, 10), ("close_camera", None, 10),
                        ("open_camera", ["ZWO ASI585MC Air"], 25)):
            try: self.cam._call(m, p, t=t)
            except Exception: pass
            time.sleep(1.5)
        try: self.cam._call("set_camera_bin", [2], t=10)
        except Exception: pass

    def count(self, exp, gain=250):
        for attempt in range(3):
            try:
                v, w, h, hdr = self.cam.grab(exp, gain)
                x = np.frombuffer(v.tobytes(), dtype="<u2").astype(np.float32).reshape(h, w)
                b = x if w <= 2200 else x[::2, ::2]
                med = float(np.median(b))
                sd = float(np.percentile(b, 84) - med) or 1.0
                mask = (b > med + 8 * sd).astype(np.uint8)
                n, lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
                good = sum(1 for i in range(1, n) if 2 <= stats[i, cv2.CC_STAT_AREA] <= 400)
                return good, med, sd, float(b.max())
            except Exception as e:
                log.warn("grab failed (%s)", type(e).__name__)
                self.recover_camera()
        return None, 0, 0, 0

    def point(self, az, alt):
        if alt < ALT_FLOOR:
            return None
        lst = self.state()["sidereal_time"]
        ra, dec = altaz_to_radec(az, alt, lst)
        s = self.goto(ra, dec)
        if s["Alt"] < ALT_FLOOR - 1:
            log.warn("landed below floor (%.1f) — skipping", s["Alt"])
            return None
        exp = 2.0 if s["Alt"] >= 25 else (4.0 if s["Alt"] >= 12 else 6.0)
        n, med, sd, mx = self.count(exp)
        if n is None:
            return None
        rec = dict(want_az=round(az, 2), want_alt=round(alt, 2),
                   az=round(s["Az"], 3), alt=round(s["Alt"], 3),
                   sources=n, exp=exp, median=round(med, 1), sigma=round(sd, 1),
                   max=round(mx), t=time.strftime("%H:%M:%S"))
        with open(self.out, "a") as f:
            f.write(json.dumps(rec) + "\n")
        return rec

    def close(self):
        try: self._m("scope_move", ["none"])
        finally:
            try: self.cam.close()
            finally: self.m.close()
