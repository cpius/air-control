#!/usr/bin/env python3
"""Fallback recorder: pull 4800 preview frames into a SER file, client-side.

Use `video.py` without `--preview-ser` instead whenever you can -- the Air has
its own AVI recorder (`start_record_avi`) which writes full sensor pixels at
sensor frame rates. This path exists for the cases that recorder cannot serve:
firmware that predates it, or wanting the frames on the laptop as they arrive
rather than on eMMC.

Its ceiling is low and worth stating plainly. The 4800 preview is always
~1472 px wide whatever the bin, produced by SUBSAMPLING 2x2 Bayer superpixels
out of the sensor (one quad kept in every 2.6 -- the mosaic survives as a hard
2-px checker, so decimation, not averaging). Measured ~1 fps on 2.4 GHz.
`begin_streaming` (push) measured 2.3x faster than `get_current_img` (pull),
so push is the default.

Duplicate frames are dropped by checksum. The Air re-serves the same preview
frame when asked faster than the camera produces, and duplicates in a SER are
worse than a shorter file: the stacker weights repeats as independent samples
and the noise stops averaging down.
"""

import hashlib
import io
import os
import struct
import time
import zipfile

from airlog import get_logger
from air_rpc import Air
from main_image import MainImage

log = get_logger("video-ser")

MAIN_PORT = 4700
COLOR = {"MONO": 0, "RGGB": 8, "GRBG": 9, "GBRG": 10, "BGGR": 11}
_EPOCH_TICKS = 621355968000000000        # 1970-01-01 in .NET 100 ns ticks


def _ticks(unix_seconds):
    return int(_EPOCH_TICKS + unix_seconds * 10_000_000)


class SerWriter:
    """SER v3: 178-byte header, frames, then a UTC timestamp trailer.

    FrameCount is unknown until we stop, so the header is written with a
    placeholder and patched on close. An interrupted recording therefore still
    leaves every captured frame on disk needing only the count fixed, rather
    than buffering a gigabyte in RAM to learn the count first.
    """

    def __init__(self, path, width, height, bits, color_id,
                 observer="", instrument="", telescope=""):
        self.path, self.width, self.height, self.bits = path, width, height, bits
        self.color_id = color_id
        self.count = 0
        self.stamps = []
        # Held on the instance: close() rewrites the header, and anything only
        # passed to __init__ would be silently lost there.
        self.meta = (observer, instrument, telescope)
        self.f = open(path, "wb")
        self.f.write(self._header(0))

    def _header(self, count):
        observer, instrument, telescope = self.meta
        now = time.time()
        h = bytearray()
        h += b"LUCAM-RECORDER"
        h += struct.pack("<i", 0)                      # LuID
        h += struct.pack("<i", self.color_id)
        # SER v3: 0 = 16-bit data big-endian, 1 = little-endian. We write native
        # little-endian. Some readers invert this; if a 16-bit file looks like
        # noise in the stacker, this flag is the first thing to flip.
        h += struct.pack("<i", 1)
        h += struct.pack("<i", self.width)
        h += struct.pack("<i", self.height)
        h += struct.pack("<i", self.bits)
        h += struct.pack("<i", count)
        for s in (observer, instrument, telescope):
            h += s.encode("utf-8", "replace")[:40].ljust(40, b"\x00")
        h += struct.pack("<q", _ticks(now))
        h += struct.pack("<q", _ticks(now))
        assert len(h) == 178, len(h)
        return bytes(h)

    def add(self, data, stamp):
        self.f.write(data)
        self.stamps.append(_ticks(stamp))
        self.count += 1

    def close(self):
        for t in self.stamps:
            self.f.write(struct.pack("<q", t))
        self.f.seek(0)
        self.f.write(self._header(self.count))
        self.f.close()
        return self.count


def unzip_frame(payload):
    if payload[:2] != b"PK":
        return payload
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        return z.read(z.namelist()[0])


def record(args, bits=16, color="RGGB", pull=False):
    exp_us = int(args.exposure_ms * 1000)
    air = Air(args.host, MAIN_PORT, key=args.key)
    if not air.verified:
        raise SystemExit("4700 handshake failed — check --key")

    def call(m, p=None, t=25):
        r = air.call(m, p or [], timeout=t)
        return r.get("result", r.get("error"))

    cams = call("get_connected_cameras") or []
    main_cam = max(cams, key=lambda c: c.get("chip_size", [0, 0])[0], default=None)
    call("stop_exposure")
    time.sleep(0.5)
    # The focus page is the fast lane: the preview page plate-solves and
    # annotates every frame (2.2 s -> 0.35 s per frame with it off).
    call("set_page", ["focus"])
    call("set_control_value", ["Exposure", exp_us])
    call("set_control_value", ["Gain", args.gain])
    expbin = call("get_camera_exp_and_bin") or {}

    img = MainImage(args.host)
    call("start_exposure", ["light"])
    time.sleep(args.exposure_ms / 1000.0 + 1.2)

    base = os.path.join(args.outdir,
                        f"{time.strftime('%Y-%m-%d-%H%M%S')}-{args.target}")
    ser_path = base + ".ser"
    writer = None
    dup = short = 0
    seen = set()
    t0 = time.time()
    if not pull:
        img.send(2, "begin_streaming")
    log.info("recording %.0fs (%s mode) -> %s", args.seconds,
             "pull" if pull else "push", ser_path)

    try:
        while time.time() - t0 < args.seconds:
            if pull:
                img.send(0, "get_current_img")
            hdr, payload = img.read_frame()
            if hdr["id"] == 1 or hdr["length"] < 1000:
                short += 1
                continue
            raw = unzip_frame(payload)
            h = hashlib.md5(raw).digest()
            if h in seen:
                dup += 1
                continue
            seen.add(h)
            if writer is None:
                writer = SerWriter(ser_path, hdr["width"], hdr["height"], bits,
                                   COLOR[color],
                                   instrument=(main_cam or {}).get("name", ""),
                                   telescope=f"FL {call('get_focal_length')}mm")
            if bits == 8:
                raw = raw[1::2]        # 12-bit data arrives left-shifted in 16
            writer.add(raw, time.time())
            if writer.count % 10 == 0:
                el = time.time() - t0
                print(f"  {writer.count} frames  {el:5.1f}s  "
                      f"{writer.count/max(el,1e-3):.2f} fps  {dup} dup", flush=True)
    except KeyboardInterrupt:
        log.info("interrupted — closing the file cleanly")
    finally:
        if not pull:
            try:
                img.send(3, "stop_streaming")
            except Exception:
                pass
        try:
            call("stop_exposure")
        except Exception:
            pass

    el = time.time() - t0
    n = writer.close() if writer else 0
    img.close()
    air.close()
    if not n:
        log.error("no frames recorded — is the camera exposing and the target in frame?")
        return
    with open(base + ".txt", "w") as f:
        f.write(f"[{(main_cam or {}).get('name','?')}]\n"
                f"Exposure = {args.exposure_ms}ms\nGain = {args.gain}\n"
                f"Bin = {expbin.get('bin')}\n"
                f"Capture Area Size = {writer.width} * {writer.height}\n"
                f"Output Format = *.SER\nDepth = {bits}bit\nColour Format = {color}\n"
                f"FrameCount = {n}\nDuration = {el:.1f} s\nFPS = {n/el:.2f}\n"
                f"Duplicates dropped = {dup}\n"
                f"Source = ASIAIR 4800 preview (subsampled ~2.6x from sensor)\n")
    print(f"\n  {n} frames in {el:.1f}s = {n/el:.2f} fps "
          f"({dup} duplicates dropped, {short} status frames)")
    print(f"  -> {ser_path}  ({os.path.getsize(ser_path)/1e6:.1f} MB)")
