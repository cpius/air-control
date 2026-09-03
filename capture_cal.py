#!/usr/bin/env python3
"""Capture calibration frames via 4700/4800 and write them as FITS locally.

The Air only writes to its own eMMC through the autosave page, which then has to
be pulled back over SMB. For calibration frames it is simpler and faster to grab
through the native image socket and write the FITS here, where the header can be
filled in correctly -- notably GAIN and the frame type, which is what makes a
dark or flat usable months later.
"""
import os, sys, time, argparse
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from starhunt import Camera

KEY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "embedded_key.pem")

def write_fits(path, arr, hdr):
    cards = []
    def card(k, v, c=""):
        if isinstance(v, bool): val = "T" if v else "F"
        elif isinstance(v, (int, np.integer)): val = "%20d" % v
        elif isinstance(v, float): val = "%20.8G" % v
        else: val = "'%-8s'" % v
        s = "%-8s= %20s" % (k, val)
        if c: s = s + " / " + c
        cards.append(s[:80].ljust(80))
    card("SIMPLE", True); card("BITPIX", 16); card("NAXIS", 2)
    card("NAXIS1", arr.shape[1]); card("NAXIS2", arr.shape[0])
    card("BZERO", 32768); card("BSCALE", 1)
    for k, v in hdr.items(): card(k, v)
    cards.append("END".ljust(80))
    head = "".join(cards)
    head += " " * ((2880 - len(head) % 2880) % 2880)
    data = (arr.astype(np.int32) - 32768).astype(">i2").tobytes()
    data += b"\0" * ((2880 - len(data) % 2880) % 2880)
    with open(path, "wb") as f:
        f.write(head.encode("ascii")); f.write(data)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=("flat", "dark", "bias", "darkflat"))
    ap.add_argument("--exp", type=float, required=True)
    ap.add_argument("--gain", type=int, default=252)
    ap.add_argument("--count", type=int, default=25)
    ap.add_argument("--out", required=True)
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST", "192.168.1.35"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    cam = Camera(a.host, KEY, "ZWO ASI585MC Air")
    cam._call("stop_exposure"); time.sleep(0.4)
    cam._call("set_page", ["preview"]); time.sleep(0.6)
    cam._call("set_camera_bin", [1]); time.sleep(0.4)
    itype = {"flat": "FLAT", "dark": "DARK", "bias": "BIAS", "darkflat": "DARK"}[a.kind]
    # Sensor temperature: get_control_value("Temperature") is GARBAGE on fw 43.97
    # (returned -41, then 65/86/114 within seconds; get_controls lists it as
    # min 0 max 1, so it is not a Celsius channel). The Temperature EVENT is the
    # real reading, so take it from the event stream instead.
    def sensor_temp(timeout=8.0):
        import time as _t
        cam.air.drain_events(); t0=_t.time(); val=None
        while _t.time()-t0 < timeout:
            for e in cam.air.drain_events():
                if e.get("Event") == "Temperature": val = e.get("value")
            if val is not None: return float(val)
            _t.sleep(0.5)
        return None
    temp0 = sensor_temp()
    print("  sensor temperature: %s C" % ("unknown" if temp0 is None else "%.1f" % temp0), flush=True)
    meds = []
    for i in range(1, a.count + 1):
        try:
            v, w, h, _ = cam.grab(a.exp, a.gain)
        except Exception as e:
            print("  %3d/%d FAILED %s" % (i, a.count, str(e)[:50]), flush=True); continue
        arr = np.frombuffer(v, dtype=np.uint16).reshape(h, w)
        med = float(np.median(arr)); meds.append(med)
        name = "%s_%.3fs_g%d_%03d.fit" % (a.kind, a.exp, a.gain, i)
        write_fits(os.path.join(a.out, name), arr,
                   {"EXPTIME": a.exp, "GAIN": a.gain, "XBINNING": 1, "YBINNING": 1,
                    "CCD-TEMP": (temp0 if temp0 is not None else -999.0),
                    "IMAGETYP": itype, "INSTRUME": "ZWO ASI585MC Air",
                    "XPIXSZ": 2.9, "YPIXSZ": 2.9, "BAYERPAT": "RGGB"})
        drift = "" if len(meds) < 2 else "  drift %+.1f%%" % (100*(med-meds[0])/max(1.0, meds[0]-962))
        print("  %3d/%d  median %6.0f ADU (%.0f%% FW)%s" % (i, a.count, med, 100*med/65535, drift), flush=True)
    if meds:
        print("\n%d frames, median %.0f ADU, spread %.1f%%"
              % (len(meds), np.median(meds), 100*(max(meds)-min(meds))/max(1.0, np.median(meds)-962)))
    cam.close()

if __name__ == "__main__":
    main()
