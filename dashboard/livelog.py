#!/usr/bin/env python3
"""One live page for a session: the tail of the session log, the newest PNG under
telemetry/, and a clock -- refreshed every 2 s without reloading. Stand-in for the
dashboard.py that vanished on 2026-09-14.

    python3 -u dashboard/livelog.py --log ../telemetry/2026-09-16_session.log --bind 0.0.0.0 --port 8765
"""
import argparse, glob, json, os, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))   # ~/ASICAP, above the repo
ap = argparse.ArgumentParser()
ap.add_argument("--log", required=True)
ap.add_argument("--bind", default="0.0.0.0")
ap.add_argument("--port", type=int, default=8765)
ap.add_argument("--png-dir", default=os.path.join(ROOT, "telemetry"), help="newest .png under here (recursive) is shown")
a = ap.parse_args()
LOG = os.path.abspath(a.log); PNGDIR = os.path.abspath(a.png_dir)

PAGE = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>ASICAP live</title><style>
body{margin:0;background:#111;color:#ddd;font:13px/1.35 ui-monospace,Menlo,monospace}
#top{padding:6px 10px;background:#222;position:sticky;top:0;display:flex;gap:16px;flex-wrap:wrap}
#wrap{display:flex;flex-wrap:wrap}
#log{flex:1 1 520px;white-space:pre-wrap;padding:8px 10px;max-height:calc(100vh - 40px);overflow:auto;word-break:break-all}
#img{flex:1 1 400px;padding:8px}#img img{max-width:100%;background:#000}
</style></head><body>
<div id=top><span id=clock></span><span id=file></span><span id=png></span></div>
<div id=wrap><div id=log>waiting...</div><div id=img><img id=frame alt=""></div></div>
<script>
let lastPng="";
async function tick(){
  try{
    const r=await fetch('/api?n=250',{cache:'no-store'}); const j=await r.json();
    const el=document.getElementById('log'); const atBottom=el.scrollHeight-el.scrollTop-el.clientHeight<60;
    el.textContent=j.tail; if(atBottom) el.scrollTop=el.scrollHeight;
    document.getElementById('clock').textContent=new Date().toLocaleTimeString()+' | log age '+j.log_age_s+' s';
    document.getElementById('file').textContent=j.log;
    if(j.png){ document.getElementById('png').textContent=j.png+' ('+j.png_age_s+' s old)';
      if(j.png_key!==lastPng){ lastPng=j.png_key; document.getElementById('frame').src='/png?k='+encodeURIComponent(j.png_key); } }
  }catch(e){ document.getElementById('clock').textContent='fetch failed: '+e; }
}
setInterval(tick,2000); tick();
</script></body></html>"""

def tail(path, n):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 300_000)); data = f.read()
        return "\n".join(data.decode("utf-8", "replace").splitlines()[-n:]), time.time() - os.path.getmtime(path)
    except FileNotFoundError:
        return "(log not found: %s)" % path, -1

_scan = [0.0, None]
def newest_png():
    if time.time() - _scan[0] > 2.0:
        best = None
        for p in glob.glob(os.path.join(PNGDIR, "**", "*.png"), recursive=True):
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if best is None or m > best[1]:
                best = (p, m)
        _scan[0] = time.time(); _scan[1] = best
    return _scan[1]

class H(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def _send(self, code, ctype, body):
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode())
        elif u.path == "/api":
            n = int(q.get("n", ["200"])[0]); t, age = tail(LOG, n); p = newest_png()
            j = {"log": os.path.relpath(LOG, ROOT), "tail": t, "log_age_s": round(age), "png": None}
            if p:
                j.update(png=os.path.relpath(p[0], ROOT), png_age_s=round(time.time() - p[1]), png_key=f"{p[0]}|{p[1]}")
            self._send(200, "application/json", json.dumps(j).encode())
        elif u.path == "/png":
            k = os.path.abspath(unquote(q.get("k", [""])[0]).split("|")[0])
            if not k.startswith(PNGDIR + os.sep) or not k.endswith(".png") or not os.path.exists(k):
                self._send(404, "text/plain", b"no"); return
            with open(k, "rb") as f:
                self._send(200, "image/png", f.read())
        else:
            self._send(404, "text/plain", b"no")

srv = ThreadingHTTPServer((a.bind, a.port), H)
print(f"{time.strftime('%H:%M:%S')} livelog on http://{a.bind}:{a.port}/  log {LOG}  pngs under {PNGDIR}", flush=True)
srv.serve_forever()
