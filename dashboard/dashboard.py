#!/usr/bin/env python3
"""The rig dashboard: weather, focus, preview, commands out, messages in.

Reads what `recorder.py` writes and serves it as a page that refreshes itself.
Nothing here talks to the Air -- it is strictly a reader, so it can be left
running across an Air restart, a power cycle, or a crash of whatever tool was
driving, and it will simply pick the story back up. That separation is the
point: the dashboard must never be able to disturb the session it is watching.

Five panes, in the order they matter at 2 a.m.:

    sky        conditions now, the 30-minute outlook, radar, Sun and Moon
    focus      the most recent sweep: V-curve, per-step measurements, closeups
    preview    the recent frames off the camera, newest first, paged
    commands   every request sent to the Air, with round-trip time and errors
    feedback   every message the Air sent back, split by subsystem

`--root` bounds what may be served. Image paths arrive from a log file and are
resolved against it, so a path outside the root is refused rather than read;
without that the page would be an arbitrary local-file reader on the LAN.

    python3 dashboard.py                       # http://localhost:8765
    python3 dashboard.py --port 9000 --bind 0.0.0.0    # reachable from the phone
    python3 dashboard.py --once > state.json   # one state dump, no server
"""

import argparse
import datetime
import glob
import heapq
import http.server
import json
import mimetypes
import os
import socketserver
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from airlog import add_log_args, configure_logging, get_logger

log = get_logger("dash")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.expanduser("~/ASICAP")
DATA = os.path.join(ROOT, "dashboard", "data")

MAX_COMMANDS = 300         # newest kept; the log on disk stays complete
MAX_EVENTS_PER_DEVICE = 120
WEATHER_TTL = 300          # seconds; Open-Meteo updates on ~15-minute steps
DEVICE_ORDER = ["mount", "camera", "guide", "focuser", "solver", "system", "app", "other"]


# ---------------------------------------------------------------------------
# Reading the event log
# ---------------------------------------------------------------------------

def _logs(data_dir):
    return sorted(glob.glob(os.path.join(data_dir, "events-*.jsonl")))


def read_events(data_dir, tail_bytes=6_000_000):
    """Newest log file, last `tail_bytes` of it, parsed.

    Tailing rather than reading whole: a night of guiding at 2 Hz is a few
    hundred thousand lines, and the dashboard only ever shows the recent end.
    A partial first line from landing mid-record is dropped.
    """
    files = _logs(data_dir)
    if not files:
        return [], None
    path = files[-1]
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > tail_bytes:
            f.seek(size - tail_bytes)
            f.readline()
        raw = f.read()
    rows = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows, path


def build_commands(rows):
    """Pair each request with its reply. Unmatched requests are still in flight
    (or were never answered), which is exactly what you want to see."""
    out, index = [], {}
    for r in rows:
        k = r.get("kind")
        if k == "cmd":
            e = {"t": r.get("t"), "port": r.get("port"), "id": r.get("id"),
                 "method": r.get("method"), "params": r.get("params"),
                 "device": r.get("device"), "state": "pending",
                 "dt": None, "result": None, "error": None}
            index[(r.get("port"), r.get("id"))] = e
            out.append(e)
        elif k == "reply":
            e = index.get((r.get("port"), r.get("id")))
            if e is None:                     # reply whose request predates the tail
                e = {"t": r.get("t"), "port": r.get("port"), "id": r.get("id"),
                     "method": r.get("method"), "params": None,
                     "device": r.get("device")}
                out.append(e)
            e["dt"] = r.get("dt")
            e["state"] = "ok" if r.get("ok") else "error"
            e["result"] = r.get("result")
            e["error"] = r.get("error")
            e["code"] = r.get("code")
        elif k == "note":
            out.append({"t": r.get("t"), "device": r.get("device"), "state": "note",
                        "method": r.get("text"), "params": None, "dt": None})
    return out[-MAX_COMMANDS:]


def build_feedback(rows):
    """Events grouped by subsystem, newest last, with the chatty ones counted."""
    by = {d: [] for d in DEVICE_ORDER}
    noise = {}
    for r in rows:
        if r.get("kind") != "event":
            continue
        d = r.get("device", "other")
        by.setdefault(d, [])
        if r.get("noisy"):
            key = (d, r.get("event"))
            noise[key] = noise.get(key, 0) + 1
        by[d].append({"t": r.get("t"), "event": r.get("event"),
                      "data": r.get("data"), "noisy": bool(r.get("noisy")),
                      "port": r.get("port"), "air_time": r.get("air_time")})
    return ({d: v[-MAX_EVENTS_PER_DEVICE:] for d, v in by.items() if v},
            {"%s/%s" % k: v for k, v in noise.items()},
            {d: len(v) for d, v in by.items() if v})


# ---------------------------------------------------------------------------
# Focus
# ---------------------------------------------------------------------------

def find_focus_run(rows, roots):
    """Most recent sweep: an explicit artifact record wins, else newest on disk."""
    for r in reversed(rows):
        if r.get("kind") == "artifact" and r.get("artifact") == "focus_run":
            if os.path.isdir(r.get("path", "")):
                return r["path"]
    best, best_m = None, -1
    for root in roots:
        for j in glob.glob(os.path.join(root, "**", "step*.json"), recursive=True):
            d = os.path.dirname(j)
            m = os.path.getmtime(j)
            if m > best_m:
                best, best_m = d, m
    return best


def read_focus_run(d):
    if not d or not os.path.isdir(d):
        return None
    steps = []
    for j in sorted(glob.glob(os.path.join(d, "step*.json"))):
        try:
            rec = json.load(open(j))
        except Exception:
            continue
        png = j[:-5] + ".png"
        steps.append({
            "step": rec.get("step"), "label": rec.get("label"),
            "position": rec.get("focuser", rec.get("requested")),
            "requested": rec.get("requested"),
            "width_px": rec.get("star_width_px"), "peak": rec.get("peak"),
            "flux": rec.get("flux"), "score": rec.get("score"),
            "jump_px": rec.get("jump_px"), "rejected": rec.get("rejected"),
            "exposure_s": rec.get("exposure_s"), "gain": rec.get("gain"),
            "star": [rec.get("star_x"), rec.get("star_y")],
            "time": rec.get("time"),
            "png": png if os.path.exists(png) else None,
        })
    steps.sort(key=lambda s: (s["position"] is None, s["position"] or 0))
    good = [s for s in steps if s["width_px"] and not s["rejected"]]
    best = min(good, key=lambda s: s["width_px"]) if good else None
    sheet = next((p for p in (os.path.join(d, "sweep.jpg"),
                              os.path.join(d, "sweep.png")) if os.path.exists(p)), None)
    return {
        "dir": d, "name": os.path.basename(d.rstrip("/")),
        "mtime": datetime.datetime.fromtimestamp(os.path.getmtime(d)).isoformat(
            timespec="seconds"),
        "steps": steps, "n": len(steps),
        "rejected": sum(1 for s in steps if s["rejected"]),
        "best": best, "contact_sheet": sheet,
        "vertex": _vertex([(s["position"], s["width_px"]) for s in good]),
    }


def _vertex(pts):
    """Parabola through the minimum and its neighbours -- the same estimate
    focus.py makes, recomputed here so the pane is readable on its own."""
    pts = sorted(p for p in pts if p[0] is not None and p[1] is not None)
    if len(pts) < 3:
        return None
    i = min(range(len(pts)), key=lambda k: pts[k][1])
    i = max(1, min(len(pts) - 2, i))
    (x1, y1), (x2, y2), (x3, y3) = pts[i - 1], pts[i], pts[i + 1]
    d = (x1 - x2) * (x1 - x3) * (x2 - x3)
    if d == 0:
        return None
    a = (x3 * (y2 - y1) + x2 * (y1 - y3) + x1 * (y3 - y2)) / d
    b = (x3 * x3 * (y1 - y2) + x2 * x2 * (y3 - y1) + x1 * x1 * (y2 - y3)) / d
    if a == 0:
        return None
    return round(-b / (2 * a), 1)


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

PREVIEW_EXT = (".jpg", ".jpeg", ".png")
MAX_PREVIEWS = 300         # newest first; the pane pages back through these


def find_previews(rows, roots, limit=MAX_PREVIEWS):
    """Preview frames, newest first. The pane shows as many as fit and pages
    back through the rest.

    Two sources, merged by time: frames a tool announced with
    rec.artifact("preview", ...), which carry its meta, and the newest images
    anywhere under the roots, which catch every tool that does not announce.
    An announced frame replaces its own glob hit; a path announced twice is
    one frame, since the file on disk only holds the later one.
    """
    announced = {}
    for r in rows:                        # oldest first: the last announcement wins
        if r.get("kind") == "artifact" and r.get("artifact") == "preview":
            announced[os.path.abspath(r.get("path", ""))] = r
    found = {}
    for root in roots:
        for ext in PREVIEW_EXT:
            for p in glob.glob(os.path.join(root, "**", "*" + ext), recursive=True):
                # Skip the log directory only -- dashboard/demo is legitimate
                # sample data and must stay findable.
                if os.path.join("dashboard", "data") + os.sep in p:
                    continue
                try:
                    found[os.path.abspath(p)] = os.path.getmtime(p)
                except OSError:
                    continue      # deleted between the glob and the stat
    for p, r in announced.items():        # may sit outside the roots' glob
        if p in found or os.path.exists(p):
            found[p] = _epoch(r.get("t")) or found.get(p) or 0
    out = []
    for p, m in heapq.nlargest(limit, found.items(), key=lambda kv: kv[1]):
        r = announced.get(p)
        out.append({"path": p, "meta": r.get("meta") if r else None,
                    "t": r.get("t") if r else
                    datetime.datetime.fromtimestamp(m).isoformat(timespec="seconds")})
    return out


def _epoch(iso):
    try:
        return datetime.datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Weather, cached so the page can poll freely without hammering the API
# ---------------------------------------------------------------------------

_wx = {"at": 0, "data": None}
_wx_lock = threading.Lock()


def weather(force=False):
    with _wx_lock:
        if not force and _wx["data"] and time.time() - _wx["at"] < WEATHER_TTL:
            return _wx["data"]
    try:
        import weather as wx
        d = wx.snapshot(with_radar=True)
    except Exception as e:
        log.warn("weather fetch failed: %s", e)
        d = (_wx["data"] or {}) | {"error": str(e), "stale": True}
    with _wx_lock:
        _wx["at"], _wx["data"] = time.time(), d
    return d


# ---------------------------------------------------------------------------

def build_state(data_dir=DATA, roots=(ROOT,), with_weather=True):
    rows, path = read_events(data_dir)
    fb, noise, counts = build_feedback(rows)
    focus_dir = find_focus_run(rows, roots)
    previews = find_previews(rows, roots)
    last = rows[-1].get("t") if rows else None
    return {
        "now": datetime.datetime.now().isoformat(timespec="seconds"),
        "log": {"file": path, "rows": len(rows), "last_record": last,
                "age_s": _age(last)},
        "weather": weather() if with_weather else None,
        "commands": build_commands(rows),
        "feedback": fb, "noise": noise, "event_counts": counts,
        "device_order": DEVICE_ORDER,
        "focus": read_focus_run(focus_dir),
        "preview": previews[0] if previews else None,     # snapshot.py reads this
        "previews": previews,
        "sessions": [r for r in rows if r.get("kind") == "session"][-5:],
    }


def _age(iso):
    if not iso:
        return None
    try:
        return round((datetime.datetime.now()
                      - datetime.datetime.fromisoformat(iso)).total_seconds(), 1)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    data_dir = DATA
    roots = (ROOT,)
    server_version = "asicap-dashboard/1"

    def log_message(self, fmt, *a):
        log.trace("http %s", fmt % a)

    def _send(self, code, body, ctype="application/json", extra=None,
              cache="no-store"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass          # the page navigated away mid-response; not a fault

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            f = os.path.join(HERE, "dashboard.html")
            if not os.path.exists(f):
                return self._send(500, "dashboard.html missing next to dashboard.py",
                                  "text/plain")
            return self._send(200, open(f, "rb").read(), "text/html; charset=utf-8")
        if u.path == "/api/state":
            try:
                return self._send(200, json.dumps(
                    build_state(self.data_dir, self.roots), default=str))
            except Exception as e:
                log.warn("state build failed: %s", e)
                return self._send(500, json.dumps({"error": str(e)}))
        if u.path == "/img":
            q = urllib.parse.parse_qs(u.query)
            return self._image(q.get("p", [""])[0], versioned="t" in q)
        return self._send(404, json.dumps({"error": "not found"}))

    def _image(self, p, versioned=False):
        """Serve a local image, but only from inside a configured root.

        The paths come out of a log file, so they are not trusted input. Both
        the root and the target are realpath'd before comparison so a symlink
        or a ../ cannot walk out.

        `versioned` means the page put the frame's timestamp in the URL, so a
        rewrite of the file is a new URL and the browser may keep this one --
        paging the preview strip then does not re-download every frame.
        """
        if not p:
            return self._send(400, json.dumps({"error": "no path"}))
        real = os.path.realpath(p)
        if not any(real.startswith(os.path.realpath(r) + os.sep) for r in self.roots):
            log.warn("refused image outside root: %s", real)
            return self._send(403, json.dumps({"error": "outside root"}))
        if not os.path.isfile(real):
            return self._send(404, json.dumps({"error": "no such file"}))
        ctype = mimetypes.guess_type(real)[0] or "application/octet-stream"
        if not ctype.startswith("image/"):
            return self._send(415, json.dumps({"error": "not an image"}))
        with open(real, "rb") as f:
            return self._send(200, f.read(), ctype,
                              cache="private, max-age=86400" if versioned else "no-store")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--bind", default="127.0.0.1",
                   help="0.0.0.0 to reach it from the phone on the balcony")
    p.add_argument("--data", default=DATA, help="where recorder.py writes")
    p.add_argument("--root", action="append", default=None,
                   help="directory images may be served from (repeatable)")
    p.add_argument("--once", action="store_true", help="dump one state as JSON and exit")
    p.add_argument("--no-weather", action="store_true")
    add_log_args(p)
    a = p.parse_args()
    configure_logging(a)
    roots = tuple(os.path.realpath(os.path.expanduser(r)) for r in (a.root or [ROOT]))

    if a.once:
        print(json.dumps(build_state(a.data, roots, not a.no_weather),
                         indent=1, default=str))
        return 0

    Handler.data_dir, Handler.roots = a.data, roots
    if not a.no_weather:                 # warm the cache before the first request
        threading.Thread(target=weather, daemon=True).start()
    with Server((a.bind, a.port), Handler) as srv:
        log.info("dashboard on http://%s:%d  (data %s, roots %s)",
                 a.bind if a.bind != "0.0.0.0" else "localhost", a.port,
                 a.data, ", ".join(roots))
        if a.bind == "127.0.0.1":
            log.info("bind 0.0.0.0 to open it to the phone on the LAN")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
