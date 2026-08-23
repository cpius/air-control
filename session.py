#!/usr/bin/env python3
"""A long-lived Air session: sockets that stay up, frames that are actually new.

Anything that runs for more than a few seconds against an Air hits the same
three walls, and each one fails in a way that points somewhere else:

  * **An abandoned capture wedges the Air.** 4800 answers `get_current_img`
    with the literal string `there is no image now`, forever, and the next
    `start_exposure` returns **206 "capture is active"** while
    `get_camera_state` still reports `idle`. Nothing recovers on its own. Every
    page/exposure change here issues `stop_exposure` FIRST for that reason.

  * **Idle sockets are dropped -- on 4400 as well as 4700.** Waiting on a 4800
    download counts as idle on both, and a full-field frame takes longer than
    the ~15 s timeout, so the socket dies mid-wait and the *next* call raises
    BrokenPipeError far from the cause. A background thread pokes both, and
    calls reconnect rather than raise.

  * **Frames repeat.** `imageID` is a constant, so a new download is only
    identifiable by content. `fresh()` hashes a subsample and re-kicks the
    capture if the Air keeps serving the same bytes.

Also here because it has no other home: a stdlib PNG writer, so a frame can be
looked at without adding a dependency.

    from session import Session
    s = Session(host, key)
    try:
        s.page("focus", 2.0, 250, binning=2)
        v, w, h = s.fresh(2)
    finally:
        s.close()
"""
import argparse
import array
import math
import os
import struct
import sys
import threading
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from air_rpc import Air
from main_image import MainImage
from mount import Mount

MAIN_PORT = 4700


class Session:
    """Camera (4700), image socket (4800) and mount (4400), kept alive."""

    def __init__(self, host, key, with_mount=True):
        self.host = host
        self.key = key
        self.air = Air(host, MAIN_PORT, key=key)
        if not self.air.verified:
            raise RuntimeError("4700 handshake failed -- check the key")
        self.img = MainImage(host)
        self.mt = Mount(host) if with_mount else None
        self.sig = None
        self._lock = threading.Lock()
        self._mt_lock = threading.Lock()
        self._stop = threading.Event()
        self._ka = threading.Thread(target=self._keepalive, daemon=True)
        self._ka.start()

    # -- plumbing ---------------------------------------------------------

    def _keepalive(self):
        while not self._stop.wait(5.0):
            try:
                self.c("get_camera_state", t=8)
            except Exception:
                pass
            if self.mt is None:
                continue
            try:
                with self._mt_lock:
                    self.mt.state()
            except Exception:
                try:
                    with self._mt_lock:
                        self.mt = Mount(self.host)
                except Exception:
                    pass

    def c(self, m, p=None, t=25, tries=3):
        """One 4700 call, reconnecting the socket if it has been dropped."""
        for i in range(tries):
            try:
                with self._lock:
                    r = self.air.call(m, p or [], timeout=t)
                return r.get("result", r.get("error"))
            except Exception:
                if i == tries - 1:
                    raise
                time.sleep(0.6)
                with self._lock:
                    try:
                        self.air.close()
                    except Exception:
                        pass
                    self.air = Air(self.host, MAIN_PORT, key=self.key)

    def mount(self):
        """The mount handle, reconnected if its socket has gone."""
        with self._mt_lock:
            try:
                self.mt.state()
            except Exception:
                self.mt = Mount(self.host)
            return self.mt

    # -- camera -----------------------------------------------------------

    def page(self, name, exp_s, gain, binning=1):
        """Select a page and start a capture. `stop_exposure` first, always."""
        self.c("stop_exposure")
        time.sleep(1.0)
        self.c("set_page", [name])
        self.c("set_camera_bin", [int(binning)])
        self.set_exp(exp_s, gain)
        self.c("start_exposure")

    def set_exp(self, exp_s, gain):
        self.c("set_control_value", ["Exposure", int(exp_s * 1_000_000)])
        self.c("set_control_value", ["Gain", int(gain)])

    def _one(self, tries=80):
        for _ in range(tries):
            hdr, files = self.img.get_image("get_current_img", 0)
            raw = next(iter(files.values()))
            sig = hash(raw[::4001])
            if sig != self.sig:
                self.sig = sig
                buf = array.array("H")
                buf.frombytes(raw)
                if sys.byteorder == "big" and not hdr["isBigEndian"]:
                    buf.byteswap()
                return buf, hdr["width"], hdr["height"]
            time.sleep(0.15)
        raise RuntimeError("no fresh frame")

    def fresh(self, n=2, kicks=3):
        """`n` frames whose content has actually changed since the last one.

        A stall is re-kicked rather than raised: the usual cause is a capture
        that stopped, not a camera that is broken.
        """
        got = None
        for _ in range(n):
            for _k in range(kicks):
                try:
                    got = self._one()
                    break
                except RuntimeError:
                    self.c("stop_exposure")
                    time.sleep(0.8)
                    self.c("start_exposure")
                    time.sleep(1.0)
            else:
                raise RuntimeError("camera will not produce frames")
            self.c("get_camera_state")          # keepalive
        return got

    def close(self):
        self._stop.set()
        try:
            self.img.close()
        finally:
            self.air.close()


# -- PNG, stdlib only -----------------------------------------------------

def write_png(path, W, H, rgb):
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        raw += rgb[y * W * 3:(y + 1) * W * 3]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
        f.write(chunk(b"IEND", b""))
    return path


def png(v, w, h, path, shrink=1, mark=None, crosshair=True):
    """Autostretched grey PNG of a u16 frame, optionally marked."""
    W, H = w // shrink, h // shrink
    px = [0] * (W * H)
    for y in range(H):
        row = (y * shrink) * w
        for x in range(W):
            px[y * W + x] = v[row + x * shrink]
    s = sorted(px[::7])
    lo = s[len(s) // 2]
    hi = max(s[int(len(s) * 0.9995)], lo + 1)
    g = bytearray(W * H * 3)
    for i, p in enumerate(px):
        t = min(255, int(255 * max(0.0, (p - lo) / float(hi - lo)) ** 0.5))
        g[3 * i] = g[3 * i + 1] = g[3 * i + 2] = t

    def dot(x, y, col):
        if 0 <= x < W and 0 <= y < H:
            j = 3 * (y * W + x)
            g[j], g[j + 1], g[j + 2] = col

    if crosshair:
        cx, cy = W // 2, H // 2
        for d in range(4, 15):
            for c in ((cx - d, cy), (cx + d, cy), (cx, cy - d), (cx, cy + d)):
                dot(c[0], c[1], (255, 0, 0))
    if mark:
        mx, my = mark[0] // shrink, mark[1] // shrink
        for a in range(0, 360, 3):
            dot(mx + int(20 * math.cos(math.radians(a))),
                my + int(20 * math.sin(math.radians(a))), (0, 255, 0))
    return write_png(path, W, H, bytes(g))


def main():
    ap = argparse.ArgumentParser(description="Grab one frame off the Air as a PNG.")
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--key", default="embedded_key.pem")
    ap.add_argument("--page", default="focus", choices=["focus", "preview"])
    ap.add_argument("--exp", type=float, default=2.0)
    ap.add_argument("--gain", type=int, default=100)
    ap.add_argument("--bin", type=int, default=1)
    ap.add_argument("--out", default="frame.png")
    a = ap.parse_args()
    if not a.host:
        sys.exit("need --host or ASIAIR_HOST (the Air's IP moves -- run discover.py)")
    s = Session(a.host, a.key, with_mount=False)
    try:
        s.page(a.page, a.exp, a.gain, a.bin)
        v, w, h = s.fresh(2)
        print("%dx%d -> %s" % (w, h, png(v, w, h, a.out)))
    finally:
        s.close()


if __name__ == "__main__":
    main()
