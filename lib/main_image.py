#!/usr/bin/env python3
"""Native ASIAIR MainImageSocket client — port 4800. No Alpaca, no RSA on this port.

Protocol (reverse-engineered from com.zwoasi.kit.socket.MainImageSocket /
ImageSocketRuntime and com.wss.rxscoketclient.SocketObservable / HeaderData):

  Commands: plain JSON lines, exactly like 4700 --
      {"id":<id>,"method":"<cmd>"}\\r\\n        (optional ,"params":<json>)
    get_current_img id=0 | get_stacked_img id=0 | get_auto_focus_img id=98
    begin_streaming id=2 | stop_streaming id=3 | test_connection id=1

  The server expects a heartbeat -- {"id":1,"method":"test_connection"} every
  4 s -- and resets the connection if the client stays silent. That is why a
  bare connect-and-listen gets RST.

  Responses: a fixed 80-BYTE header, big-endian, then `length` payload bytes.
      [0:2]   magic  (u16)  == 963
      [2:4]   version(u16)
      [6:10]  length (i32)  payload byte count
      [12]    isBigEndian   [13] imgType   [14] dataType   [15] id
      [16:18] width  (u16)  [18:20] height (u16)
      [20:22] hfdX [22:24] hfdY [24:26] hfd [26:28] canDebayer [28:30] imageID
    id==1 -> heartbeat payload (discard). Otherwise the payload is a ZIP
    archive containing the image file.
"""
import argparse, io, json, os, socket, struct, sys, threading, time, zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from airlog import add_log_args, configure_logging, get_logger

log = get_logger("image")

HOST = os.environ.get("ASIAIR_HOST")
PORT = 4800
MAGIC = 963
HDR = 80


def _human(n):
    """Byte count as KB/MB — download lines are read at a glance, mid-session."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


def build_cmd(cmd_id, method, params=None):
    if params is None:
        return ('{"id":%d,"method":"%s"}\r\n' % (cmd_id, method)).encode()
    return ('{"id":%d,"method":"%s","params":%s}\r\n'
            % (cmd_id, method, json.dumps(params))).encode()


def parse_header(h):
    """Decode the 80-byte image header. Shared with the guide stream on 4500 —
    both sockets are `com.zwoasi.kit.socket.*ImageSocket` over the same
    `ImageSocketRuntime` and the one `HeaderData` class, so the framing is
    identical. The hfd fields only carry meaning on the guide side."""
    magic = struct.unpack(">H", h[0:2])[0]
    if magic != MAGIC:
        raise IOError(f"bad magic {magic} (expected {MAGIC})")
    return {
        "magic": magic,
        "version": struct.unpack(">H", h[2:4])[0],
        "length": struct.unpack(">i", h[6:10])[0],
        "isBigEndian": h[12], "imgType": h[13], "dataType": h[14], "id": h[15],
        "width": struct.unpack(">H", h[16:18])[0],
        "height": struct.unpack(">H", h[18:20])[0],
        "hfdX": struct.unpack(">H", h[20:22])[0],
        "hfdY": struct.unpack(">H", h[22:24])[0],
        "hfd": struct.unpack(">H", h[24:26])[0],
        "canDebayer": struct.unpack(">H", h[26:28])[0],
        "imageID": struct.unpack(">H", h[28:30])[0],
    }


class MainImage:
    def __init__(self, host=HOST, port=PORT, timeout=180):
        self.host, self.port = host, port
        with log.slow("connect %s:%d" % (host, port), quiet_for=1.0):
            self.s = socket.create_connection((host, port), timeout=15)
        self.s.settimeout(timeout)
        log.debug("4800 connected to %s (read timeout %ss); heartbeat every 4s",
                  host, timeout)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._beats = 0
        self._hb = threading.Thread(target=self._heartbeat, daemon=True)
        self._hb.start()

    def _heartbeat(self):
        # Mandatory: 4800 resets a client that stays silent for ~4s. If it ever
        # dies the socket follows within seconds, so say so loudly -- otherwise
        # the next download just "hangs" for no visible reason.
        while not self._stop.wait(4.0):
            try:
                with self._lock:
                    self.s.sendall(build_cmd(1, "test_connection"))
                self._beats += 1
                log.trace("4800 heartbeat #%d", self._beats)
            except Exception as e:
                log.warn("4800 heartbeat failed after %d beat(s): %s — the Air "
                         "will drop this socket", self._beats, e)
                return

    def send(self, cmd_id, method, params=None):
        log.debug("4800 -> id=%d %s %s", cmd_id, method, params if params else "")
        with self._lock:
            self.s.sendall(build_cmd(cmd_id, method, params))

    def _recv_exact(self, n):
        """Read exactly n bytes, reporting throughput every second.

        A full-frame payload is several MB over Wi-Fi, so this is one of the
        genuinely multi-second operations in the toolkit — and the one where a
        stalled transfer is least visible, because a half-received frame looks
        exactly like a slow one until the read timeout fires minutes later.
        """
        buf = bytearray()
        t0 = time.time()
        with log.slow("receive %s" % _human(n), quiet_for=1.0,
                      detail=lambda: "%s / %s  (%s/s)"
                                     % (_human(len(buf)), _human(n),
                                        _human(len(buf) / max(time.time() - t0, 1e-3)))):
            while len(buf) < n:
                chunk = self.s.recv(min(65536, n - len(buf)))
                if not chunk:
                    log.error("4800 socket closed with %d of %d bytes read",
                              len(buf), n)
                    raise ConnectionError("socket closed mid-frame")
                buf += chunk
        dt = time.time() - t0
        if n > 262144:
            log.debug("received %s in %.2fs (%s/s)", _human(n), dt,
                      _human(n / max(dt, 1e-3)))
        return bytes(buf)

    def read_frame(self):
        """Return (header_dict, payload_bytes)."""
        hdr = parse_header(self._recv_exact(HDR))
        length = hdr["length"]
        log.trace("4800 frame id=%d %dx%d dataType=%d %s",
                  hdr["id"], hdr["width"], hdr["height"], hdr["dataType"],
                  _human(length))
        payload = self._recv_exact(length) if length > 0 else b""
        return hdr, payload

    def get_image(self, method="get_current_img", cmd_id=0, wait=120):
        """Send a download command and return (header, unzipped_files dict)."""
        self.send(cmd_id, method)
        t0 = time.time()
        skipped = [0]
        with log.slow("download %s" % method, quiet_for=1.0,
                      detail=lambda: "%d heartbeat/status frame(s) skipped so far"
                                     % skipped[0]) as tk:
            while time.time() - t0 < wait:
                hdr, payload = self.read_frame()
                if hdr["id"] == 1:                      # heartbeat echo
                    skipped[0] += 1
                    continue
                if hdr["length"] in (4, 21) and hdr["width"] == 0:
                    skipped[0] += 1
                    tk.event("short/status frame: %r", payload[:32])
                    print("   (short/status frame:", payload[:32], ")")
                    continue
                files = {}
                if payload[:2] == b"PK":
                    tk.set("unzipping %s" % _human(len(payload)))
                    with zipfile.ZipFile(io.BytesIO(payload)) as z:
                        for n in z.namelist():
                            files[n] = z.read(n)
                    log.debug("unzipped %d file(s): %s", len(files),
                              ", ".join("%s %s" % (k, _human(len(v)))
                                        for k, v in files.items()))
                else:
                    files["<raw>"] = payload
                log.debug("%s -> %dx%d, %s in %.2fs", method, hdr["width"],
                          hdr["height"], _human(len(payload)), time.time() - t0)
                return hdr, files
        log.error("no image frame from %s within %ss (%d frames skipped)",
                  method, wait, skipped[0])
        raise TimeoutError("no image frame")

    def close(self):
        log.debug("closing 4800 to %s after %d heartbeat(s)", self.host, self._beats)
        self._stop.set()
        try:
            self.s.close()
        except Exception:
            pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--method", default="get_current_img")
    ap.add_argument("--id", type=int, default=0)
    ap.add_argument("--out", default="native_img")
    add_log_args(ap)
    a = ap.parse_args()
    configure_logging(a)

    m = MainImage(a.host)
    print(f"connected to {a.host}:{PORT}; requesting {a.method} ...")
    try:
        hdr, files = m.get_image(a.method, a.id)
        print("header:", json.dumps(hdr))
        for name, data in files.items():
            path = f"{a.out}_{os.path.basename(name) or 'payload.bin'}"
            with open(path, "wb") as f:
                f.write(data)
            print(f"  {name}: {len(data)} bytes -> {path}  head={data[:24]!r}")
    finally:
        m.close()
