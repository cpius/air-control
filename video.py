#!/usr/bin/env python3
"""Record a planetary video on an ASIAIR, using the firmware's OWN recorder.

The Air records AVI to its eMMC. The commands are `start_record_avi` and
`stop_record_avi`, and the ROI is `set_subframe` -- names taken from the ASIAIR
app's own command table (`com.zwoasi.kit.cmd.CmdMethod` paired with
`MainCameraConstants`; see CMD_METHODS.tsv, 289 entries). Probing for these by
guessing names finds nothing: `start_video`, `start_recording`,
`start_video_record` and `set_roi` all return `103 method not found`, which
looks exactly like the feature being absent. It is not. Read the app, not the
error code.

WHY THIS BEATS PULLING PREVIEW FRAMES. The 4800 preview is a fixed ~1472 px
wide subsample of the sensor (one 2x2 Bayer quad kept in every 2.6) and runs at
~1 fps over 2.4 GHz Wi-Fi. The Air's own recorder writes full sensor pixels at
sensor frame rates straight to eMMC, and `set_subframe` crops the READOUT, so a
small ROI around a planet is both full resolution and fast. That is the
difference between Saturn on ~34 px and Saturn on ~89 px (or far more at
2032 mm), and between ~30 frames in 30 s and thousands.

Sequence:

    set_page(["video"])            # the video tab; 'video' is a real page name
    set_subframe({x,y,w,h})        # optional ROI, in SENSOR pixels
    set_control_value Exposure/Gain
    start_exposure(["light"])      # free-run the camera
    start_record_avi               # NO parameters
    ... AviRecord events: {is_working, lapse_sec, fps, write_file_fps} ...
    stop_record_avi                # NO parameters

The AVI lands on the Air's eMMC; pull it over the SMB share
(//<air-ip>/EMMC Images) or the app's file browser.

    python3 video.py --host <air-ip> --key embedded_key.pem \
        --seconds 30 --exposure-ms 8 --gain 250 --roi 800x800

    python3 video.py --host <air-ip> --key embedded_key.pem --roi-full
    python3 video.py --host <air-ip> --key embedded_key.pem --preview-ser   # old path

NOTE: the native path below is reconstructed from the app and has NOT yet been
run against a live Air (the battery died before it could be tested). The
sequence and names are read off the app's own gateway code, but the exact
param shape for `set_subframe` is the one guess left -- Gson serialises the
ROIBean as a bare object, while most Air commands take a single-element list,
so both are attempted and the working one is reported. `102` vs `107` is the
Air's param-shape oracle: `107` means stop list-wrapping, `102` means start.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from air_rpc import Air
from airlog import add_log_args, configure_logging, get_logger

log = get_logger("video")

MAIN_PORT = 4700


class Rig:
    def __init__(self, host, key):
        self.host, self.key = host, key
        self.air = Air(host, MAIN_PORT, key=key)
        if not self.air.verified:
            raise SystemExit("4700 handshake failed — check --key")

    def call(self, m, p=None, t=25, quiet=False):
        for attempt in range(3):
            try:
                r = self.air.call(m, p if p is not None else [], timeout=t)
                v = r.get("result", r.get("error"))
                if not quiet:
                    log.info("-> %s(%s)  <- %s", m, json.dumps(p) if p is not None else "[]",
                             json.dumps(v, ensure_ascii=False)[:180])
                return v, r.get("code")
            except Exception as e:
                log.warn("%s failed (%s) — reconnecting %d/3", m, e, attempt + 1)
                try:
                    self.air.close()
                except Exception:
                    pass
                time.sleep(0.6)
                self.air = Air(self.host, MAIN_PORT, key=self.key)
        raise RuntimeError(f"{m} failed")

    def set_subframe(self, x, y, w, h):
        """Try both param shapes; the Air's error code says which it wanted.

        Gson writes the app's ROIBean as a bare object, but most Air commands
        take a single-element list. Rather than guess, send one, and on a
        param-shape error (102/105/107) send the other.
        """
        obj = {"x": int(x), "y": int(y), "width": int(w), "height": int(h)}
        for params in ([obj], obj):
            v, code = self.call("set_subframe", params)
            if code in (102, 105, 107) or (isinstance(v, str) and "param" in v.lower()):
                log.info("param shape rejected (code %s) — trying the other form", code)
                continue
            return v
        raise RuntimeError("set_subframe rejected both param shapes")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ)
    ap.add_argument("--key", required=True)
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--exposure-ms", type=float, default=8.0)
    ap.add_argument("--gain", type=int, default=250)
    ap.add_argument("--roi", default=None,
                    help="ROI as WxH (centred) or XxY+WxH; sensor pixels. "
                         "A small ROI is the whole point: full resolution AND fast.")
    ap.add_argument("--roi-full", action="store_true", help="clear the ROI to the full sensor")
    ap.add_argument("--preview-ser", action="store_true",
                    help="legacy path: record the 4800 preview to a SER here "
                         "(subsampled 2.6x, ~1 fps) instead of using the Air's recorder")
    ap.add_argument("--target", default="planet")
    ap.add_argument("--outdir", default=".")
    add_log_args(ap)
    args = ap.parse_args()
    configure_logging(args)

    if args.preview_ser:
        import video_preview_ser
        return video_preview_ser.record(args)

    rig = Rig(args.host, args.key)
    exp_us = int(args.exposure_ms * 1000)

    cams, _ = rig.call("get_connected_cameras")
    main_cam = max(cams or [], key=lambda c: c.get("chip_size", [0, 0])[0], default=None)
    chip = (main_cam or {}).get("chip_size", [0, 0])
    log.info("main camera %s chip=%s", (main_cam or {}).get("name"), chip)

    rig.call("stop_exposure")
    time.sleep(0.5)
    rig.call("set_page", ["video"])

    if args.roi_full:
        rig.set_subframe(0, 0, chip[0], chip[1])
    elif args.roi:
        if "+" in args.roi:
            xy, wh = args.roi.split("+", 1)
            x, y = (int(v) for v in xy.lower().split("x"))
            w, h = (int(v) for v in wh.lower().split("x"))
        else:
            w, h = (int(v) for v in args.roi.lower().split("x"))
            x, y = (chip[0] - w) // 2, (chip[1] - h) // 2
        rig.set_subframe(x, y, w, h)
    rig.call("get_subframe")

    rig.call("set_control_value", ["Exposure", exp_us])
    rig.call("set_control_value", ["Gain", args.gain])
    rig.call("get_camera_exp_and_bin")

    rig.air.drain_events()
    rig.call("start_exposure", ["light"])
    time.sleep(args.exposure_ms / 1000.0 + 1.0)

    log.info("recording %.0fs", args.seconds)
    rig.call("start_record_avi")
    t0 = time.time()
    last = {}
    try:
        while time.time() - t0 < args.seconds:
            for e in rig.air.drain_events():
                if e.get("Event") in ("AviRecord", "VideoCapture"):
                    last = e
                    print(f"  [{e.get('Event')}] working={e.get('is_working')} "
                          f"lapse={e.get('lapse_sec')}s fps={e.get('fps')} "
                          f"write_fps={e.get('write_file_fps')}", flush=True)
            # 4700 drops an idle socket at ~15 s; a silent 30 s record would
            # disconnect before stop_record_avi could be sent.
            rig.call("get_camera_state", quiet=True)
            time.sleep(1.0)
    except KeyboardInterrupt:
        log.info("interrupted — stopping the recording cleanly")
    finally:
        rig.call("stop_record_avi")
        rig.call("stop_exposure")

    el = time.time() - t0
    fps = last.get("write_file_fps") or last.get("fps")
    print(f"\n  recorded ~{el:.1f}s"
          + (f" at {fps} fps (~{int(float(fps)*el)} frames)" if fps else "")
          + "\n  the AVI is on the Air's eMMC — pull it from the SMB share "
            f"//{args.host}/'EMMC Images' or the app's file browser")
    rig.air.close()


if __name__ == "__main__":
    main()
