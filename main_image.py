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

HOST = os.environ.get("ASIAIR_HOST")
PORT = 4800
MAGIC = 963
HDR = 80


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
        self.s = socket.create_connection((host, port), timeout=15)
        self.s.settimeout(timeout)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._hb = threading.Thread(target=self._heartbeat, daemon=True)
        self._hb.start()

    def _heartbeat(self):
        while not self._stop.wait(4.0):
            try:
                with self._lock:
                    self.s.sendall(build_cmd(1, "test_connection"))
            except Exception:
                return

    def send(self, cmd_id, method, params=None):
        with self._lock:
            self.s.sendall(build_cmd(cmd_id, method, params))

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.s.recv(min(65536, n - len(buf)))
            if not chunk:
                raise ConnectionError("socket closed mid-frame")
            buf += chunk
        return bytes(buf)

    def read_frame(self):
        """Return (header_dict, payload_bytes)."""
        hdr = parse_header(self._recv_exact(HDR))
        length = hdr["length"]
        payload = self._recv_exact(length) if length > 0 else b""
        return hdr, payload

    def get_image(self, method="get_current_img", cmd_id=0, wait=120):
        """Send a download command and return (header, unzipped_files dict)."""
        self.send(cmd_id, method)
        t0 = time.time()
        while time.time() - t0 < wait:
            hdr, payload = self.read_frame()
            if hdr["id"] == 1:                      # heartbeat echo
                continue
            if hdr["length"] in (4, 21) and hdr["width"] == 0:
                print("   (short/status frame:", payload[:32], ")")
                continue
            files = {}
            if payload[:2] == b"PK":
                with zipfile.ZipFile(io.BytesIO(payload)) as z:
                    for n in z.namelist():
                        files[n] = z.read(n)
            else:
                files["<raw>"] = payload
            return hdr, files
        raise TimeoutError("no image frame")

    def close(self):
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
    a = ap.parse_args()

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
