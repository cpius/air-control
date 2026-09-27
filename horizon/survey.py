#!/usr/bin/env python3
"""Survey the skyline by star-counting on a grid of true (az, alt).

Classifier: count point sources. On a clear night an open field returns dozens;
a wall returns the fixed hot-pixel floor of ~6-7 and nothing more. That is the
method that produced the 2026-08-15 arcs, and it is far more robust than
background brightness, which twilight and city glow confound.

Pointing: `scope_goto` and `scope_sync` are both broken on this rig (300 / 207),
so this drives the axes directly and keeps the register->true mapping in
software. RA and Dec moves are decoupled in the register frame, so no Jacobian
is needed -- just two independent closed loops.

Results are appended to JSONL as they are measured, so a dropped Wi-Fi link
costs one point rather than the whole run.
"""
import json, math, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import numpy as np
import cv2
from air_rpc import Air
from starhunt import Camera
from airlog import get_logger

log = get_logger("survey")
H = "192.168.1.35"
KEY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "embedded_key.pem")
LAT = 55.689444
LON = 12.555278
ALT_FLOOR = 2.5
D = math.degrees
R = math.radians


def lst_hours(air4400):
    """Local sidereal time from the mount, which now carries a correct clock."""
    i = air4400.call("scope_get_info", [], timeout=8)["result"]
    return i["sidereal_time"], i


def altaz_to_radec(az, alt, lst):
    az_r, alt_r, lat = R(az), R(alt), R(LAT)
    dec = math.asin(math.sin(alt_r) * math.sin(lat) +
                    math.cos(alt_r) * math.cos(lat) * math.cos(az_r))
    cosha = (math.sin(alt_r) - math.sin(dec) * math.sin(lat)) / (math.cos(dec) * math.cos(lat))
    ha = math.acos(max(-1.0, min(1.0, cosha)))
    if math.sin(az_r) > 0:
        ha = -ha
    return (lst - D(ha) / 15.0) % 24, D(dec)


def radec_to_altaz(ra, dec, lst):
    ha = R((lst - ra) * 15.0)
    dec_r, lat = R(dec), R(LAT)
    alt = math.asin(math.sin(dec_r) * math.sin(lat) + math.cos(dec_r) * math.cos(lat) * math.cos(ha))
    az = math.atan2(-math.sin(ha) * math.cos(dec_r),
                    math.sin(dec_r) * math.cos(lat) - math.cos(dec_r) * math.sin(lat) * math.cos(ha))
    return D(alt), D(az) % 360


class Survey:
    def __init__(self, d_ra_deg, d_dec_deg, out):
        self.dra = d_ra_deg / 15.0        # register + this = true, in hours
        self.ddec = d_dec_deg
        self.out = out
        self.m = Air(H, 4400, timeout=8)
        self.cam = Camera(H, KEY, cam_name="ZWO ASI585MC Air")
        self.rate = {7: 6.25, 4: 0.091}   # deg/s
        self._sign_dec = 1
        self._sign_ra = 1

    # --- link ---
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

    def reg(self):
        i = self._m("scope_get_info")["result"]
        return i["RA"], i["Dec"], i["Alt"], i["Az"], i["sidereal_time"]

    def poke(self):
        """Keep the 4700 camera socket alive.

        It drops after ~15 s idle, and a slew is longer than that -- so without
        this every frame pays a re-handshake, which on a weak link costs 20-60 s
        and dominates the whole survey.
        """
        try:
            self.cam._call("get_camera_state", t=6, tries=2)
        except Exception:
            pass

    def move(self, d, deg, idx):
        if deg <= 0: return
        self._m("scope_set_slew_rate", [idx])
        secs = min(9.0, deg / self.rate[idx])
        self._m("scope_move", [d])
        t0 = last = time.time()
        while time.time() - t0 < secs:
            time.sleep(0.03)
            if time.time() - last > 5.0:
                self.poke(); last = time.time()
        self.stop_verified()
        self.poke()

    def stop_verified(self):
        """Re-issue stop until the axes actually stop moving.

        A lost stop is the worst failure here: the mount runs on, a 1 deg
        request becomes 9 deg, and a naive sign-detector reads the overshoot as
        a reversed axis and thrashes. Confirm by reading position twice.
        """
        for _ in range(5):
            self._m("scope_move", ["none"])
            time.sleep(0.7)
            b = self.reg(); time.sleep(1.0); c = self.reg()
            if abs(c[1] - b[1]) < 0.03 and abs((c[0] - b[0]) * 15) < 0.06:
                return True
            log.warn("stop was lost -- mount still moving, re-issuing")
        return False

    def to_register(self, ra_t, dec_t, tol_deg=0.25, maxiter=12):
        """Drive the register to a target, RA and Dec independently.

        The direction->register-sign mapping is NOT fixed: it flips with pier
        side, so "north" can decrease Dec. Assuming otherwise sent a drive to
        alt 6 when it was asked for alt 70, and silently voided a whole survey.
        So the sign is measured from the first move on each axis and flipped if
        the error grew.
        """
        sign = {"dec": self._sign_dec, "ra": self._sign_ra}
        for k in range(maxiter):
            ra, dec, alt, az, lst = self.reg()
            dra = (ra_t - ra) * 15.0
            while dra > 180: dra -= 360
            while dra < -180: dra += 360
            ddec = dec_t - dec
            if abs(dra) < tol_deg and abs(ddec) < tol_deg:
                return True
            for axis, amt, pos, neg in (("dec", ddec, "north", "south"),
                                        ("ra", dra, "east", "west")):
                if abs(amt) < tol_deg:
                    continue
                want = amt * sign[axis]
                d = pos if want > 0 else neg
                idx = 7 if abs(amt) > 1.5 else 4
                before = self.reg()
                self.move(d, min(abs(amt), 55.0), idx)
                after = self.reg()
                if axis == "dec":
                    got = after[1] - before[1]
                else:
                    got = (after[0] - before[0]) * 15.0
                    while got > 180: got -= 360
                    while got < -180: got += 360
                if abs(got) > 0.15 and (got > 0) != (amt > 0):
                    sign[axis] *= -1
                    if axis == "dec": self._sign_dec = sign[axis]
                    else: self._sign_ra = sign[axis]
                    log.warn("%s direction sign flipped (asked %+.2f, got %+.2f)", axis, amt, got)
        return False

    # --- classifier ---
    def set_bin(self, b):
        """bin 2 quarters the download. On a weak link that is the whole survey:
        a full-frame 7.6 MB grab takes 33 s at 240 KB/s, bin 2 takes about 8."""
        try:
            self.cam._call("set_camera_bin", [int(b)], t=10)
        except Exception:
            log.warn("could not set bin %d", b)

    def count_sources(self, exp=2.0, gain=250):
        v, w, h, hdr = self.cam.grab(exp, gain)
        x = np.frombuffer(v.tobytes(), dtype="<u2").astype(np.float32).reshape(h, w)
        b = x if w <= 2200 else x[::2, ::2]   # already binned? do not decimate again
        med = float(np.median(b))
        sd = float(np.percentile(b, 84) - med) or 1.0
        mask = (b > med + 8 * sd).astype(np.uint8)
        n, lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
        good = sum(1 for i in range(1, n) if 2 <= stats[i, cv2.CC_STAT_AREA] <= 400)
        return good, med, sd, float(b.max())

    def point(self, az, alt):
        """Go to a true (az, alt) and classify it."""
        _, _, _, _, lst = self.reg()
        ra_t, dec_t = altaz_to_radec(az, alt, lst)
        ok = self.to_register(ra_t - self.dra, dec_t - self.ddec)
        ra, dec, ralt, raz, lst2 = self.reg()
        t_alt, t_az = radec_to_altaz(ra + self.dra, dec + self.ddec, lst2)
        if t_alt < ALT_FLOOR:
            return None
        # Extinction at low altitude wipes out faint stars, so an OPEN field
        # low down looks like a blocked one. Expose longer to compensate.
        exp = 2.0 if t_alt >= 25 else (4.0 if t_alt >= 12 else 6.0)
        n, med, sd, mx = self.count_sources(exp=exp)
        rec = dict(want_az=round(az, 2), want_alt=round(alt, 2),
                   az=round(t_az, 2), alt=round(t_alt, 2),
                   sources=n, exp=exp, median=round(med, 1), sigma=round(sd, 1), max=round(mx),
                   converged=ok, t=time.strftime("%H:%M:%S"))
        with open(self.out, "a") as f:
            f.write(json.dumps(rec) + "\n")
        return rec

    def close(self):
        try: self._m("scope_move", ["none"])
        finally:
            self.cam.close(); self.m.close()
