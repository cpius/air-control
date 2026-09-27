#!/usr/bin/env python3
"""Watch the guide camera live, on TCP 4500 — without disturbing the main camera.

The app shows both sensors side by side, and this is how: the guide camera has
its own image socket. `GuideImageSocket` and `MainImageSocket` are siblings in
`com.zwoasi.kit.socket`, sharing one `ImageSocketRuntime` and one `HeaderData`,
so the framing is the 80-byte big-endian header of `main_image.py` (magic 963),
reused here rather than re-derived.

**This replaces the camera-swap workaround.** Looking through the guide sensor
previously meant releasing it from 4400, opening it on 4700 by name, exposing,
and putting both halves back — which interrupts imaging and risks leaving the
rig half-configured. Nothing needs to move now: the main camera keeps running.

Working sequence, verified live 2026-08-16 against firmware 43.97:

    4400: set_camera_idx([0])                 # FIRST — see below
    4400: set_connected([{"camera": true}])
    4400: loop()                              # guide camera starts exposing
    4500: connect, then begin_streaming       # this script

Three things that are not obvious, each of which looks like a broken socket:

* **`set_camera_idx` must come first.** Without it `set_connected` still returns
  0, but the sensor is not attached and `loop` fails `303 could not start
  looping`.
* **`begin_streaming` is required.** Connecting to 4500 and waiting yields
  silence forever, even with the guide camera happily looping. There is no
  polling equivalent: `get_current_img` and `get_star_image` are both answered
  `unknown mothod` (the firmware's own spelling), so this socket is stream-only.
* **`set_exposure` does not affect a running stream.** Change it and frames keep
  arriving at the old exposure; `stop_capture` then `loop` to apply it.

Unlike 4800, **4500 does not need a heartbeat** — a client that stays silent for
15 s is not dropped. It is off by default here for that reason.

Frames are **640x360 8-bit raw** (230400 bytes, `dataType` 2) — a downscaled
preview of the 960x540 sensor, not full-resolution data. `hfd`/`hfdX`/`hfdY`
read 0/65535/65535 while no star is selected; they carry the Air's own guide
star measurement once one is locked, which is what makes this useful for
checking guide-side focus rather than only for looking.

    python3 guide_image.py --host <air-ip>              # stream frame stats
    python3 guide_image.py --host <air-ip> --save 3     # save the first 3 frames
    python3 guide_image.py --host <air-ip> --loop       # bring the stream up first
"""

import argparse
import io
import os
import socket
import sys
import threading
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from air_rpc import Air
from airlog import add_log_args, configure_logging, get_logger
from main_image import HDR, build_cmd, parse_header

log = get_logger("guidecam")

PORT = 4500
MOUNT_PORT = 4400


class GuideImage:
    def __init__(self, host, port=PORT, timeout=60, heartbeat=False):
        self.host, self.port = host, port
        self.frames_seen = 0
        with log.slow("connect %s:%d" % (host, port), quiet_for=1.0):
            self.s = socket.create_connection((host, port), timeout=15)
        self.s.settimeout(timeout)
        log.debug("4500 connected (read timeout %ss, heartbeat=%s)",
                  timeout, heartbeat)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # 4800 resets a client that stays silent for ~4s; 4500 does not — a
        # 15s silence was measured with the stream still alive afterwards. Off
        # by default; the option remains in case firmware changes.
        self._hb = None
        if heartbeat:
            self._hb = threading.Thread(target=self._heartbeat, daemon=True)
            self._hb.start()

    def _heartbeat(self):
        while not self._stop.wait(4.0):
            try:
                with self._lock:
                    self.s.sendall(build_cmd(1, "test_connection"))
            except Exception:
                return

    def _recv_exact(self, n, what="bytes"):
        buf = bytearray()
        t0 = time.time()
        with log.slow("waiting for %d %s" % (n, what), quiet_for=1.0,
                      detail=lambda: "%d/%d received — is the guide camera "
                                     "looping?" % (len(buf), n)):
            while len(buf) < n:
                chunk = self.s.recv(min(65536, n - len(buf)))
                if not chunk:
                    log.error("4500 closed with %d of %d bytes read", len(buf), n)
                    raise ConnectionError("socket closed mid-frame")
                buf += chunk
        log.trace("read %d %s in %.2fs", n, what, time.time() - t0)
        return bytes(buf)

    def read_frame(self):
        """Return (header, payload). Blocks until a frame arrives.

        The header read is the one that can block for a long time: with no
        `begin_streaming`, or with the guide camera not looping, nothing ever
        arrives and the socket simply stays quiet. Hence the per-second tick.
        """
        hdr = parse_header(self._recv_exact(HDR, "header bytes"))
        payload = (self._recv_exact(hdr["length"], "payload bytes")
                   if hdr["length"] > 0 else b"")
        return hdr, payload

    def begin_streaming(self):
        """Start the frame flow. Nothing arrives without this — connecting and
        waiting yields silence forever, even with the guide camera looping.
        `get_current_img` / `get_star_image` are answered `unknown mothod`
        (the firmware's own spelling), so this socket is stream-only."""
        log.info("begin_streaming on 4500 — nothing arrives without this")
        with self._lock:
            self.s.sendall(build_cmd(2, "begin_streaming"))

    def stop_streaming(self):
        try:
            with self._lock:
                self.s.sendall(build_cmd(3, "stop_streaming"))
        except Exception:
            pass

    def frames(self):
        """Yield (header, files) for each image frame, skipping heartbeat and
        status frames. Guide frames are raw 8-bit, so `files` is
        {'<raw>': bytes}; the ZIP branch mirrors 4800 in case that changes."""
        while True:
            hdr, payload = self.read_frame()
            if hdr["id"] == 1 or (hdr["width"] == 0 and hdr["length"] < 64):
                log.trace("4500 status/heartbeat frame, skipped")
                continue                      # "server connected!" / "done"
            self.frames_seen += 1
            files = {}
            if payload[:2] == b"PK":
                with zipfile.ZipFile(io.BytesIO(payload)) as z:
                    for n in z.namelist():
                        files[n] = z.read(n)
            else:
                files["<raw>"] = payload
            yield hdr, files

    def close(self):
        log.debug("closing 4500 after %d frame(s)", self.frames_seen)
        self.stop_streaming()
        self._stop.set()
        try:
            self.s.close()
        except Exception:
            pass


def start_looping(host):
    """Bring the guide sensor up and start the stream, on 4400. Safe to call
    when it is already looping."""
    a = Air(host, MOUNT_PORT, timeout=10)
    try:
        def call(m, p=None):
            r = a.call(m, p or [], timeout=12)
            return r.get("result"), r.get("error")
        # Attaching the guide sensor is ~3 s of mandatory sleeps, so it is one
        # of the operations that must narrate itself.
        with log.slow("bringing the guide camera up on 4400") as tk:
            state, _ = call("get_app_state")
            log.info("guide state is %s", state)
            if state in ("Looping", "Selected", "Guiding"):
                return state
            conn, _ = call("get_connected")
            if not (isinstance(conn, dict) and "camera" in conn):
                # set_camera_idx FIRST — without it set_connected appears to
                # succeed but leaves the sensor unattached, and `loop` then
                # fails with 303 "could not start looping".
                tk.set("set_camera_idx(0)")
                call("set_camera_idx", [0])
                time.sleep(1)
                tk.set("set_connected camera=True")
                call("set_connected", [{"camera": True}])
                tk.set("waiting 2s for the sensor to attach")
                time.sleep(2)
            tk.set("loop")
            res, err = call("loop")
            if err:
                log.error("loop failed: %s", err)
                raise RuntimeError(f"loop: {err} — is the guide sensor attached?")
        state = call("get_app_state")[0]
        log.info("guide camera looping, state=%s", state)
        return state
    finally:
        a.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--loop", action="store_true",
                    help="start the guide stream on 4400 first")
    ap.add_argument("--save", type=int, default=0, metavar="N",
                    help="save the first N frames and exit")
    ap.add_argument("--out", default="guide_frame", help="filename prefix for --save")
    ap.add_argument("--count", type=int, default=0,
                    help="stop after N frames (0 = run until interrupted)")
    ap.add_argument("--heartbeat", action="store_true",
                    help="send a 4s keepalive (4500 does not need one; 4800 does)")
    add_log_args(ap)
    a = ap.parse_args()
    configure_logging(a)

    if a.loop:
        print("guide state ->", start_looping(a.host))

    g = GuideImage(a.host, heartbeat=a.heartbeat)
    g.begin_streaming()
    print(f"connected to {a.host}:{PORT}, streaming "
          f"(the guide camera must be looping)")
    n = 0
    try:
        for hdr, files in g.frames():
            n += 1
            print(f"[{time.strftime('%H:%M:%S')}] frame {n}  "
                  f"{hdr['width']}x{hdr['height']}  dataType={hdr['dataType']}  "
                  f"hfd={hdr['hfd']} ({hdr['hfdX']},{hdr['hfdY']})  "
                  f"{hdr['length']} bytes", flush=True)
            log.debug("frame %d: %dx%d hfd=%d (%d,%d) %d bytes", n, hdr["width"],
                      hdr["height"], hdr["hfd"], hdr["hfdX"], hdr["hfdY"],
                      hdr["length"])
            if n <= a.save:
                for name, data in files.items():
                    leaf = "raw" if name == "<raw>" else (os.path.basename(name) or "payload.bin")
                    path = f"{a.out}_{n:03d}.{leaf}" if leaf == "raw" else f"{a.out}_{n:03d}_{leaf}"
                    with open(path, "wb") as fh:
                        fh.write(data)
                    print(f"    wrote {path} ({len(data)} bytes)")
            if a.save and n >= a.save:
                break
            if a.count and n >= a.count:
                break
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        g.close()


if __name__ == "__main__":
    sys.exit(main())
