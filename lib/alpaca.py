#!/usr/bin/env python3
"""ASCOM Alpaca client for the ASI585MC Air. Stdlib only.

Alpaca is the documented, vendor-supported REST API — ZWO advertise it for this
camera, so this is the path that won't break on a firmware update.

This Air serves Alpaca on port 32323, not the ASCOM default of 11111; that is
the default here. Pass --port to override.

    GET  http://host:32323/api/v1/camera/0/gain
    PUT  http://host:32323/api/v1/camera/0/startexposure   Duration=5&Light=True

Usage:
    python3 alpaca.py --host <air-ip> info
    python3 alpaca.py --host <air-ip> get camera 0 ccdtemperature
    python3 alpaca.py --host <air-ip> put camera 0 gain Gain=252
    python3 alpaca.py --host <air-ip> expose 5 --gain 252 --out test.fits
"""

import argparse
import itertools
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from airlog import add_log_args, configure_logging, get_logger

log = get_logger("alpaca")

CLIENT_ID = 5851
_txn = itertools.count(1)


class AlpacaError(RuntimeError):
    pass


class Alpaca:
    def __init__(self, host, port=32323, timeout=30):
        self.base = f"http://{host}:{port}"
        self.timeout = timeout

    def _call(self, method, url, params):
        params = dict(params or {})
        params.setdefault("ClientID", CLIENT_ID)
        params.setdefault("ClientTransactionID", next(_txn))
        if method == "GET":
            req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}")
        else:
            req = urllib.request.Request(
                url,
                data=urllib.parse.urlencode(params).encode(),
                method="PUT",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        t0 = time.time()
        # `imagearray` is the slow one: a full frame is 8.3M JSON numbers and
        # takes tens of seconds to transfer and parse. Every request ticks.
        try:
            with log.slow("%s %s" % (method, url.rsplit("/", 1)[-1]),
                          quiet_for=1.0,
                          detail=lambda: "waiting on %s (timeout %ss)"
                                         % (self.base, self.timeout)):
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                log.debug("%s %s -> %d bytes in %.2fs", method,
                          url.rsplit("/", 1)[-1], len(raw), time.time() - t0)
                with log.slow("parsing %.1f MB of JSON" % (len(raw) / 1048576.0),
                              quiet_for=1.0):
                    body = json.loads(raw.decode())
        except urllib.error.HTTPError as e:
            log.error("HTTP %d on %s", e.code, url)
            raise AlpacaError(f"HTTP {e.code} on {url}: {e.read()[:400]!r}") from None
        except urllib.error.URLError as e:
            log.error("cannot reach %s: %s", url, e.reason)
            raise AlpacaError(f"cannot reach {url}: {e.reason}") from None

        # Alpaca reports device errors in-band, with HTTP 200.
        if body.get("ErrorNumber"):
            log.error("device error %s: %s", body["ErrorNumber"],
                      body.get("ErrorMessage"))
            raise AlpacaError(f"device error {body['ErrorNumber']}: {body.get('ErrorMessage')}")
        return body.get("Value")

    # --- management (no device number) ---
    def management(self, what):
        return self._call("GET", f"{self.base}/management/{what}", {})

    def devices(self):
        return self.management("v1/configureddevices")

    def description(self):
        return self.management("v1/description")

    # --- device API ---
    def get(self, dev, num, prop, **params):
        return self._call("GET", f"{self.base}/api/v1/{dev}/{num}/{prop}", params)

    def put(self, dev, num, prop, **params):
        return self._call("PUT", f"{self.base}/api/v1/{dev}/{num}/{prop}", params)

    # --- camera conveniences ---
    def connect(self, num=0, dev="camera"):
        self.put(dev, num, "connected", Connected=True)
        return self.get(dev, num, "connected")

    def camera_info(self, num=0):
        keys = [
            "name", "description", "driverversion", "sensortype", "sensorname",
            "cameraxsize", "cameraysize", "pixelsizex", "pixelsizey",
            "maxbinx", "gain", "gainmin", "gainmax", "offset",
            "ccdtemperature", "cooleron", "canfastreadout", "readoutmodes",
            "bayeroffsetx", "bayeroffsety", "exposuremin", "exposuremax",
        ]
        out = {}
        # 22 sequential HTTP round trips — several seconds on a busy Air.
        done = [0]
        with log.slow("reading %d camera properties" % len(keys), quiet_for=1.0,
                      detail=lambda: "%d/%d" % (done[0], len(keys))):
            for k in keys:
                try:
                    out[k] = self.get(dev="camera", num=num, prop=k)
                except AlpacaError as e:
                    log.warn("%s unreadable: %s", k, e)
                    out[k] = f"<{e}>"
                done[0] += 1
        return out

    def expose(self, seconds, num=0, light=True, gain=None, offset=None, poll=0.5):
        """Take one frame and return it as a 2D/3D list from imagearray."""
        if gain is not None:
            self.put("camera", num, "gain", Gain=int(gain))
        if offset is not None:
            self.put("camera", num, "offset", Offset=int(offset))

        log.info("startexposure %.3fs (light=%s, gain=%s)", seconds, light, gain)
        self.put("camera", num, "startexposure", Duration=float(seconds), Light=bool(light))
        t0 = time.time()
        deadline = t0 + float(seconds) + 120
        # The exposure runs, then the Air reads out and debayers. Both are
        # invisible from here except through this poll, so narrate it.
        with log.slow("exposing %.3fs" % seconds, quiet_for=1.0,
                      detail=lambda: "%.1fs elapsed%s"
                                     % (time.time() - t0,
                                        " — exposure over, waiting on readout"
                                        if time.time() - t0 > seconds else "")):
            while time.time() < deadline:
                if self.get("camera", num, "imageready"):
                    break
                time.sleep(poll)
            else:
                log.error("imageready never went true within %.0fs",
                          float(seconds) + 120)
                raise AlpacaError("timed out waiting for imageready")
        log.info("image ready after %.1fs — downloading imagearray "
                 "(this is the slow part: millions of JSON numbers)",
                 time.time() - t0)
        return self.get("camera", num, "imagearray")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--port", type=int, default=32323)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info")

    g = sub.add_parser("get")
    g.add_argument("dev"); g.add_argument("num", type=int); g.add_argument("prop")

    p = sub.add_parser("put")
    p.add_argument("dev"); p.add_argument("num", type=int); p.add_argument("prop")
    p.add_argument("kv", nargs="*", help="Key=Value pairs, e.g. Gain=252")

    e = sub.add_parser("expose")
    e.add_argument("seconds", type=float)
    e.add_argument("--num", type=int, default=0)
    e.add_argument("--gain", type=int)
    e.add_argument("--offset", type=int)
    e.add_argument("--dark", action="store_true")
    e.add_argument("--out", help="write raw imagearray JSON here")

    add_log_args(ap)
    a = ap.parse_args()
    configure_logging(a)
    c = Alpaca(a.host, a.port)

    if a.cmd == "info":
        print("description:", json.dumps(c.description(), indent=2))
        devs = c.devices()
        print("\nconfigureddevices:", json.dumps(devs, indent=2))
        for d in devs or []:
            if d.get("DeviceType", "").lower() == "camera":
                n = d.get("DeviceNumber", 0)
                print(f"\nconnecting camera {n} ...", c.connect(n))
                print(json.dumps(c.camera_info(n), indent=2))
        return

    if a.cmd == "get":
        print(json.dumps(c.get(a.dev, a.num, a.prop), indent=2))
        return

    if a.cmd == "put":
        kw = dict(x.split("=", 1) for x in a.kv)
        print(json.dumps(c.put(a.dev, a.num, a.prop, **kw), indent=2))
        return

    if a.cmd == "expose":
        c.connect(a.num)
        t0 = time.time()
        img = c.expose(a.seconds, num=a.num, light=not a.dark,
                       gain=a.gain, offset=a.offset)
        rows = len(img)
        cols = len(img[0]) if rows else 0
        with log.slow("flattening %d rows" % rows, quiet_for=1.0):
            flat = [v for row in img for v in (row if isinstance(row, list) else [row])]
            nums = [v for v in flat if isinstance(v, (int, float))]
        print(f"got {rows}x{cols} in {time.time()-t0:.1f}s")
        if nums:
            print(f"min={min(nums)} max={max(nums)} mean={sum(nums)/len(nums):.1f}")
        if a.out:
            with log.slow("writing %s" % a.out, quiet_for=1.0):
                with open(a.out, "w") as f:
                    json.dump(img, f)
            print("wrote", a.out)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AlpacaError as e:
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)
