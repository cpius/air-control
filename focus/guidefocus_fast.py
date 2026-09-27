#!/usr/bin/env python3
"""Fast focus-page readout for the guide sensor.

The focus page FREE-RUNS: the Air keeps exposing on its own and the newest frame
is always sitting on 4800. It does not emit the per-frame `Exposure complete`
event that `starhunt.grab()` waits for, so grab() blocks for `exposure + 40s`
and then raises -- 41 seconds of nothing per frame, while the frame itself was
available in 0.2s. Pull from 4800 directly instead and detect new frames BY
CONTENT (the 4800 header's imageID is a constant, so it cannot be used).

Measured 2026-08-27: ~0.3s per frame this way against ~4s via grab(), and a
failed frame costs nothing instead of 41s.
"""
import os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from air_rpc import Air
from main_image import MainImage
import guidefocus as gf

HOST = os.environ.get("ASIAIR_HOST", "192.168.1.35")
KEY  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "embedded_key.pem")

class FocusPage:
    def __init__(self, cam_name="ZWO ASI220MM Air", binning=1):
        self.a = Air(HOST, 4700, key=KEY, timeout=15); self.a.verify(KEY)
        self._c("stop_exposure")
        st = self._c("get_camera_state")
        if not (isinstance(st, dict) and st.get("name") == cam_name):
            self._c("close_camera"); time.sleep(1.2)
            self._c("open_camera", [cam_name], t=25); time.sleep(1.2)
        self._c("set_page", ["focus"]); time.sleep(0.8)
        self._c("set_camera_bin", [binning]); time.sleep(0.5)
        self.img = MainImage(HOST)
        self.last = None

    def _c(self, m, p=None, t=15):
        try:
            r = self.a.call(m, p or [], timeout=t); return r.get("result", r.get("error"))
        except Exception as e:
            return f"EXC {e}"

    def set_exposure(self, exp_s, gain):
        self._c("set_control_value", ["Exposure", int(exp_s * 1e6)])
        self._c("set_control_value", ["Gain", int(gain)])
        self._c("start_exposure")

    def frame(self, fresh=True, timeout=8.0):
        """Newest frame. `fresh` waits until the content changes."""
        t0 = time.time()
        while True:
            hdr, files = self.img.get_image("get_current_img", 0)
            raw = next(iter(files.values()))
            sig = (len(raw), hash(raw[:4096]), hash(raw[-4096:]))
            if not fresh or self.last is None or sig != self.last or time.time() - t0 > timeout:
                self.last = sig
                a = np.frombuffer(raw, dtype=np.uint16).reshape(hdr["height"], hdr["width"])
                return a, hdr["width"], hdr["height"]
            time.sleep(0.05)

    def close(self):
        try: self.img.close()
        finally: self.a.close()
