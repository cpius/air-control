#!/usr/bin/env python3
"""Freeze the dashboard into one self-contained HTML file, for publishing.

The live dashboard only exists where `dashboard.py` runs. This makes a version
that works anywhere -- on a phone, away from the flat -- by inlining every
external asset as a data: URI: radar tiles, the basemap, the preview frame, the
focus closeups. The published page makes no network requests at all, which is
also what the Artifact CSP requires.

**The whole design problem here is that it must never be mistaken for live.** A
rig dashboard that looks current but is twenty minutes stale is worse than no
dashboard: it will be trusted. So the freeze time is the loudest element on the
page, every relative time ("4s ago") is rendered as an absolute clock time, and
the live-status chip is replaced by a stamp saying when the shutter closed.

    python3 snapshot.py                      # -> dashboard/snapshot.html
    python3 snapshot.py --from http://localhost:8765 --out /tmp/snap.html
"""

import argparse
import base64
import datetime
import html
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from airlog import add_log_args, configure_logging, get_logger

log = get_logger("snapshot")

DEFAULT_SRC = "http://localhost:8765"
DEFAULT_OUT = os.path.expanduser("~/ASICAP/dashboard/snapshot.html")

# Radar frames to carry. Each is 9 tiles, so this is the main lever on file
# size; 5 is enough to read the direction a cell is travelling.
RADAR_FRAMES = 5
FETCH_TIMEOUT = 20

DEVICE_HUE = {"mount": "#7aa2f7", "camera": "#9ece6a", "guide": "#e0af68",
              "focuser": "#bb9af7", "solver": "#7dcfff", "system": "#8896aa",
              "app": "#c0caf5", "other": "#6b7688"}


def fetch(url, timeout=FETCH_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "asicap-snapshot/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "application/octet-stream")


_cache = {}


def data_uri(url, note=""):
    """Inline any URL as base64. Returns None on failure, never raises --
    a missing tile must degrade to a gap, not to no page at all."""
    if url in _cache:
        return _cache[url]
    try:
        body, ctype = fetch(url)
        ctype = ctype.split(";")[0].strip() or "image/png"
        uri = "data:%s;base64,%s" % (ctype, base64.b64encode(body).decode())
        log.debug("inlined %s (%.0f kB) %s", url[:70], len(body) / 1024, note)
    except Exception as e:
        log.warn("could not inline %s: %s", url[:70], e)
        uri = None
    _cache[url] = uri
    return uri


def local_img(src, path):
    return data_uri(src + "/img?p=" + urllib.parse.quote(path))


# ---------------------------------------------------------------------------
# Rendering. Plain string building -- no template engine, no dependencies.
# ---------------------------------------------------------------------------

E = lambda s: html.escape(str(s if s is not None else ""))


def num(v, d=0, dash="—"):
    try:
        return dash if v is None else ("%.*f" % (d, float(v)))
    except (TypeError, ValueError):
        return dash


def clock(iso):
    return str(iso)[11:19] if iso else "—"


def head(state, frozen):
    return f"""<div class="freeze">
  <div class="freeze-mark">FROZEN</div>
  <div>
    <b>{E(frozen.strftime('%H:%M:%S'))}</b> on {E(frozen.strftime('%A %-d %B %Y'))}
    <span class="tz">Europe/Copenhagen</span>
    <p>This page does not update. It is a still frame of the rig dashboard,
       captured at the time above. Every clock time on it is from that moment.
       The live page runs on the laptop at <code>localhost:8765</code>.</p>
  </div>
</div>"""


def sky(w):
    if not w or not w.get("current"):
        return '<section class="card"><h2>Sky</h2><p class="empty">no weather in this capture</p></section>'
    c, o = w.get("current", {}), w.get("outlook", {})
    s, m = w.get("sun", {}), w.get("moon", {})
    dew = c.get("dew_risk", "unknown")
    tiles = [("cloud", num(c.get("cloud_cover")) + "%", ""),
             ("dew spread", num(c.get("dew_spread"), 1) + " K", dew),
             ("temp", num(c.get("temperature"), 1) + "°", ""),
             ("wind", num(c.get("wind")) + "", "km/h"),
             ("sun", num(s.get("altitude")) + "°", s.get("twilight", "")),
             ("moon", (num(m.get("altitude")) + "°" if m.get("up") else "down"),
              num((m.get("illumination") or 0) * 100) + "% lit")]
    cells = "".join(
        f'<div class="stat"><span>{E(k)}</span><b class="{E("risk-"+v2) if k=="dew spread" else ""}">'
        f'{E(v)}</b><i>{E(v2)}</i></div>' for k, v, v2 in tiles)
    heater = ("earns its 6.2 W" if dew in ("warning", "critical")
              else "off saves 6.2 W of a 42 W budget")
    return f"""<section class="card span2">
  <h2>Sky <span class="src">open-meteo · rainviewer · {E(clock(w.get('fetched')))}</span></h2>
  <div class="verdict v-{E(o.get('verdict','unknown'))}">
    <span class="v-label">next 30 min</span>{E(o.get('text','no data'))}</div>
  <div class="stats">{cells}</div>
  <div class="two">
    <div>
      {cloud_chart(w.get('series') or [])}
      <dl class="kv">
        <dt>dew</dt><dd><b class="risk-{E(dew)}">{E(dew)}</b> — heater {E(heater)}</dd>
        <dt>twilight</dt><dd>{E(s.get('twilight','?'))}{
          ' — the Sun never reaches −18° here between mid-May and late July'
          if s.get('twilight') == 'nautical' else ''}</dd>
        <dt>visibility</dt><dd>{num((c.get('visibility') or 0)/1000)} km, gusts
          {num(c.get('gusts'))} km/h</dd>
      </dl>
    </div>
    {radar(w.get('radar'))}
  </div>
</section>"""


def cloud_chart(series):
    if not series:
        return ""
    W, H, P = 620, 108, 26
    n = len(series)
    X = lambda i: P + i * (W - P - 10) / max(1, n - 1)
    Y = lambda v: H - 18 - (float(v or 0) / 100) * (H - 32)
    pts = [(X(i), Y(s.get("cloud"))) for i, s in enumerate(series)]
    poly = " ".join("%.1f,%.1f" % p for p in pts)
    area = ("M%.1f,%.1f " % (pts[0][0], H - 18) + " ".join("L%.1f,%.1f" % p for p in pts)
            + " L%.1f,%.1f Z" % (pts[-1][0], H - 18))
    rain = "".join(
        '<rect x="%.1f" y="%d" width="4" height="9" fill="var(--bad)"/>' % (X(i) - 2, H - 18)
        for i, s in enumerate(series) if (s.get("precip") or 0) > 0.05)
    ticks = "".join(
        '<text x="%.1f" y="%d" class="ax" text-anchor="middle">%s</text>'
        % (X(i), H - 4, E(str(s.get("time", ""))[11:16]))
        for i, s in enumerate(series) if i % 8 == 0)
    # The endpoint gets a dot: on a frozen page the far end of the series is the
    # only part still ahead of the reader, and it should be findable at a glance.
    end = '<circle cx="%.1f" cy="%.1f" r="3" fill="var(--accent)"/>' % pts[-1]
    return f"""<figure class="chart"><figcaption>cloud cover, next 12 h</figcaption>
<svg viewBox="0 0 {W} {H}" role="img" aria-label="cloud cover forecast">
  <line x1="{P}" x2="{W-10}" y1="{Y(50):.1f}" y2="{Y(50):.1f}" class="gridline"/>
  <text x="0" y="{Y(100)+4:.1f}" class="ax">100%</text>
  <text x="6" y="{Y(0)+4:.1f}" class="ax">0</text>
  <path d="{area}" class="area"/>
  <polyline points="{poly}" class="line"/>{rain}{ticks}{end}
</svg></figure>"""


def radar(r):
    if not r or not r.get("frames"):
        return '<div class="empty">no radar in this capture</div>'
    frames = r["frames"][-RADAR_FRAMES:]
    log.info("inlining radar: %d frames + basemap", len(frames))

    def layer(tiles, cls):
        out = []
        for t in tiles or []:
            uri = data_uri(t["url"])
            if not uri:
                continue
            out.append(
                '<div class="tile %s" style="background-image:url(%s);'
                'left:%s%%;top:%s%%;width:%s%%;height:%s%%"></div>'
                % (cls, uri, t["left"], t["top"], t["width"], t["height"]))
        return "".join(out)

    sets = "".join(
        f'<div class="frameset" data-k="{k}" data-t="{E(str(f.get("iso") or "")[11:])}"'
        f' data-kind="{"forecast" if f.get("nowcast") else "observed"}">'
        f'{layer(f.get("tiles"), "fr")}</div>'
        for k, f in enumerate(frames))
    sx, sy = (r.get("site_pct") or [50, 50])
    return f"""<figure class="chart">
<figcaption>radar · {E(r.get('km_across'))} km across · balcony centred</figcaption>
<div class="radar" id="radar">
  {layer(r.get('basemap'), 'base')}{sets}
  <div class="site" style="left:{sx}%;top:{sy}%"><span>here</span></div>
  <div class="stamp"><span id="rt"></span><span id="rk"></span></div>
</div></figure>"""


def focus(f):
    if not f:
        return '<section class="card"><h2>Focus</h2><p class="empty">no focus run in this capture</p></section>'
    good = [s for s in f["steps"] if s.get("width_px") and not s.get("rejected")]
    best = f.get("best") or {}
    strip = ""
    for s in f["steps"]:
        if not s.get("png"):
            continue
        uri = local_img(SRC, s["png"])
        if not uri:
            continue
        cls = "rej" if s.get("rejected") else ("best" if s.get("step") == best.get("step") else "")
        sub = s["rejected"] if s.get("rejected") else num(s.get("width_px"), 2) + " px"
        strip += (f'<figure class="{cls}"><img src="{uri}" alt="focuser {E(s.get("position"))}">'
                  f'<figcaption>{E(s.get("position"))}<br><i>{E(sub)}</i></figcaption></figure>')
    return f"""<section class="card">
  <h2>Focus <span class="src">{E(f.get('name'))} · {E(clock(f.get('mtime')))}</span></h2>
  {vcurve(f, good)}
  <dl class="kv">
    <dt>best</dt><dd><b>{E(best.get('position','—'))}</b> at {num(best.get('width_px'),2)} px
      {f'<span class="dim">· parabola vertex {E(f["vertex"])}</span>' if f.get('vertex') else ''}</dd>
    <dt>steps</dt><dd>{f['n']} measured, {f['rejected']} rejected</dd>
    <dt>frames</dt><dd>{num((good or [{}])[0].get('exposure_s'),1)} s at gain
      {E((good or [{}])[0].get('gain','—'))}</dd>
  </dl>
  <div class="strip">{strip}</div>
</section>"""


def vcurve(f, good):
    if len(good) < 3:
        return '<p class="empty">not enough usable steps to plot</p>'
    W, H, L, B = 560, 190, 44, 28
    xs = [s["position"] for s in f["steps"] if s.get("position") is not None]
    x0, x1 = min(xs), max(xs)
    ws = [s["width_px"] for s in good]
    y0, y1 = min(ws) * 0.94, max(ws) * 1.05
    X = lambda v: L + (v - x0) / max(1e-9, x1 - x0) * (W - L - 14)
    Y = lambda v: H - B - (v - y0) / max(1e-9, y1 - y0) * (H - B - 16)
    pts = [(X(s["position"]), Y(s["width_px"])) for s in good]
    dots = "".join('<circle cx="%.1f" cy="%.1f" r="3"/>' % p for p in pts)
    rej = "".join(
        '<g class="x"><line x1="%.1f" y1="%d" x2="%.1f" y2="%d"/>'
        '<line x1="%.1f" y1="%d" x2="%.1f" y2="%d"/></g>'
        % (X(s["position"]) - 4, H - B - 5, X(s["position"]) + 4, H - B - 13,
           X(s["position"]) - 4, H - B - 13, X(s["position"]) + 4, H - B - 5)
        for s in f["steps"] if s.get("rejected"))
    v = f.get("vertex")
    vx = ""
    if v is not None and x0 <= v <= x1:
        vx = (f'<line x1="{X(v):.1f}" x2="{X(v):.1f}" y1="12" y2="{H-B}" class="vtx"/>'
              f'<text x="{X(v)+5:.1f}" y="21" class="vtx-t">{E(v)}</text>')
    return f"""<figure class="chart"><figcaption>star width against focuser position</figcaption>
<svg viewBox="0 0 {W} {H}" role="img" aria-label="focus V-curve">
  <line x1="{L}" x2="{W-14}" y1="{H-B}" y2="{H-B}" class="gridline"/>
  <line x1="{L}" x2="{L}" y1="12" y2="{H-B}" class="gridline"/>
  <text x="2" y="18" class="ax">{num(y1,1)} px</text>
  <text x="2" y="{H-B}" class="ax">{num(y0,1)}</text>
  <text x="{L}" y="{H-8}" class="ax">{x0}</text>
  <text x="{W-14}" y="{H-8}" class="ax" text-anchor="end">{x1}</text>
  {vx}<polyline points="{' '.join('%.1f,%.1f' % p for p in pts)}" class="vline"/>
  <g class="vdot">{dots}</g>{rej}
</svg></figure>"""


def preview(p):
    if not p:
        return '<section class="card"><h2>Preview</h2><p class="empty">no preview frame in this capture</p></section>'
    uri = local_img(SRC, p["path"])
    body = (f'<img src="{uri}" alt="most recent frame">' if uri
            else '<p class="empty">frame could not be read</p>')
    meta = "".join(f"<dt>{E(k)}</dt><dd>{E(v)}</dd>" for k, v in (p.get("meta") or {}).items())
    return f"""<section class="card">
  <h2>Preview <span class="src">{E(clock(p.get('t')))}</span></h2>
  <div class="frame">{body}</div>
  <dl class="kv"><dt>file</dt><dd class="wrap">{E(os.path.basename(p['path']))}</dd>{meta}</dl>
</section>"""


def commands(rows):
    if not rows:
        return '<section class="card span2"><h2>Commands</h2><p class="empty">none recorded</p></section>'
    body = ""
    for r in reversed(rows):
        st = r.get("state", "ok")
        if st == "error":
            res = f'<span class="bad">{E(r.get("error"))} ({E(r.get("code"))})</span>'
        elif st == "pending":
            res = '<span class="warn">no reply</span>'
        elif st == "note":
            res = ""
        else:
            res = f'<span class="dim">{E(json.dumps(r.get("result"))[:80])}</span>'
        dev = r.get("device", "other")
        body += (f'<tr class="st-{E(st)}"><td class="dim">{E(clock(r.get("t")))}</td>'
                 f'<td><span class="dev" style="--h:{DEVICE_HUE.get(dev,"#6b7688")}">'
                 f'{E(dev)}</span></td>'
                 f'<td class="m">{"▸ " if st == "note" else ""}{E(r.get("method"))}</td>'
                 f'<td class="dim wrap">{E(json.dumps(r.get("params")) if r.get("params") is not None else "")}</td>'
                 f'<td class="n">{num(r["dt"]*1000) if r.get("dt") is not None else ""}</td>'
                 f'<td class="wrap">{res}</td></tr>')
    errs = sum(1 for r in rows if r.get("state") == "error")
    return f"""<section class="card span2">
  <h2>Commands to the Air <span class="src">{len(rows)} shown · {errs} errors</span></h2>
  <div class="tw"><table><thead><tr><th>time</th><th>device</th><th>method</th>
    <th>params</th><th class="n">ms</th><th>result</th></tr></thead>
    <tbody>{body}</tbody></table></div>
</section>"""


def feedback(state):
    fb = state.get("feedback") or {}
    order = [d for d in (state.get("device_order") or []) if fb.get(d)]
    order += [d for d in fb if d not in order]
    if not order:
        return '<section class="card span2"><h2>Messages</h2><p class="empty">none recorded</p></section>'
    tabs, panes = "", ""
    for i, d in enumerate(order):
        rows = fb[d]
        quiet = sum(1 for e in rows if e.get("noisy"))
        tabs += (f'<button class="tab{" on" if i == 0 else ""}" data-d="{E(d)}"'
                 f' style="--h:{DEVICE_HUE.get(d,"#6b7688")}">'
                 f'<span class="dot"></span>{E(d)}<i>{len(rows)}</i></button>')
        evs = "".join(
            f'<div class="ev{" quiet" if e.get("noisy") else ""}">'
            f'<span class="dim">{E(clock(e.get("t")))}</span>'
            f'<b>{E(e.get("event"))}</b>'
            f'<span class="d">{E(fmt(e.get("data")))}</span></div>'
            for e in reversed(rows))
        note = (f'<p class="fold">{quiet} routine {E(d)} message'
                f'{"s" if quiet != 1 else ""} in this capture — shown greyed'
                f'</p>' if quiet else "")
        panes += (f'<div class="pane{" on" if i == 0 else ""}" data-d="{E(d)}"'
                  f' style="--h:{DEVICE_HUE.get(d,"#6b7688")}">{note}'
                  f'<div class="evs">{evs}</div></div>')
    total = sum(len(v) for v in fb.values())
    return f"""<section class="card span2">
  <h2>Messages from the Air <span class="src">{total} in this capture,
    split by subsystem</span></h2>
  <div class="tabs">{tabs}</div>{panes}
</section>"""


def fmt(d):
    if d is None:
        return ""
    if not isinstance(d, dict):
        return str(d)[:200]
    out = []
    for k, v in d.items():
        if isinstance(v, float):
            v = "%.3f" % v
        elif not isinstance(v, (int, str)):
            v = json.dumps(v)
        out.append("%s=%s" % (k, v))
    return "  ".join(out)[:200]


# ---------------------------------------------------------------------------

CSS = """
:root{
  --bg:#0a0d12; --card:#121722; --card2:#1a2130; --line:#232c3c;
  --ink:#dfe6f2; --dim:#7f8da3; --faint:#5a6779;
  --accent:#4ea8ff; --ok:#4fd48a; --warn:#f0b849; --bad:#ef6b6b;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  --sans:"IBM Plex Sans",system-ui,-apple-system,Segoe UI,sans-serif;
}
/* Committed single theme: this is an instrument panel meant to be read at
   night beside a telescope, so it stays dark on any host ground. Every colour
   is painted explicitly rather than inherited. */
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
  font-size:14px;line-height:1.55;-webkit-text-size-adjust:100%}
.wrapper{max-width:1180px;margin:0 auto;padding:20px 16px 56px}
h1{font-family:var(--mono);font-size:15px;font-weight:600;letter-spacing:.2em;
  margin:0 0 14px;color:var(--dim);text-transform:uppercase}
h2{font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:.16em;
  text-transform:uppercase;color:var(--dim);margin:0 0 12px;
  display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
h2 .src{margin-left:auto;color:var(--faint);letter-spacing:.04em;
  text-transform:none;font-weight:400}
code{font-family:var(--mono);background:var(--card2);padding:1px 5px;border-radius:4px}
.dim{color:var(--faint)} .bad{color:var(--bad)} .warn{color:var(--warn)}
.empty{color:var(--faint);font-style:italic;padding:20px 0;text-align:center}
.wrap{overflow-wrap:anywhere}

/* the freeze stamp -- deliberately the loudest thing on the page */
.freeze{display:flex;gap:16px;align-items:flex-start;background:var(--card);
  border:1px solid var(--warn);border-left-width:4px;border-radius:10px;
  padding:14px 16px;margin-bottom:18px}
.freeze-mark{font-family:var(--mono);font-size:10px;font-weight:600;
  letter-spacing:.2em;color:var(--warn);border:1px solid var(--warn);
  border-radius:4px;padding:3px 8px;white-space:nowrap;margin-top:2px}
.freeze b{font-family:var(--mono);font-size:19px;font-variant-numeric:tabular-nums}
.freeze .tz{color:var(--faint);font-size:12px;margin-left:8px}
.freeze p{margin:6px 0 0;color:var(--dim);font-size:13px;max-width:64ch}

.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:14px 16px;min-width:0}
.span2{grid-column:1/-1}

.verdict{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap;
  border:1px solid currentColor;border-radius:8px;padding:10px 13px;
  margin-bottom:12px;font-size:14px}
.verdict .v-label{font-family:var(--mono);font-size:10px;letter-spacing:.14em;
  text-transform:uppercase;opacity:.85}
.v-clear,.v-clearing,.v-steady{color:var(--ok)}
.v-worsening,.v-closing{color:var(--warn)}
.v-rain{color:var(--bad)} .v-unknown{color:var(--faint)}

.stats{display:grid;gap:9px;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));
  margin-bottom:14px}
.stat{background:var(--card2);border-radius:7px;padding:8px 10px}
.stat span{display:block;font-family:var(--mono);font-size:9.5px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--faint)}
.stat b{display:block;font-family:var(--mono);font-size:21px;font-weight:600;
  font-variant-numeric:tabular-nums;line-height:1.25}
.stat i{font-style:normal;font-size:11px;color:var(--faint)}
.risk-clear{color:var(--ok)} .risk-warning{color:var(--warn)}
.risk-critical{color:var(--bad)}

.two{display:grid;gap:16px;align-items:start;
  grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.chart{margin:0}
.chart figcaption{font-family:var(--mono);font-size:10px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--faint);margin-bottom:6px}
.chart svg{width:100%;height:auto;display:block}
.ax{fill:var(--faint);font-family:var(--mono);font-size:9px}
.gridline{stroke:var(--line);stroke-dasharray:3 3}
.area{fill:var(--accent);opacity:.16}
.line{fill:none;stroke:var(--accent);stroke-width:1.8}
.vline{fill:none;stroke:#bb9af7;stroke-width:1.8}
.vdot circle{fill:#bb9af7}
.vtx{stroke:var(--ok);stroke-dasharray:3 3}
.vtx-t{fill:var(--ok);font-family:var(--mono);font-size:10px}
.x line{stroke:var(--bad);stroke-width:1.5}

.radar{position:relative;width:100%;max-width:340px;aspect-ratio:1;margin:0 auto;
  border-radius:8px;overflow:hidden;background:#05070a}
.radar .tile{position:absolute;background-size:100% 100%;background-repeat:no-repeat}
.radar .base{opacity:.78;filter:invert(1) grayscale(1) brightness(.92) contrast(.78)}
.radar .frameset{position:absolute;inset:0;opacity:0;transition:opacity .16s}
.radar .frameset.on{opacity:.76}
.radar .site{position:absolute;width:11px;height:11px;margin:-5.5px 0 0 -5.5px;
  border:1.5px solid var(--accent);border-radius:50%;
  box-shadow:0 0 0 1px #000a,0 0 10px var(--accent)}
.radar .site span{position:absolute;left:15px;top:-3px;font-family:var(--mono);
  font-size:9px;letter-spacing:.08em;color:var(--accent);text-shadow:0 0 4px #000}
.radar .stamp{position:absolute;left:0;right:0;bottom:0;padding:4px 8px;
  background:#000b;font-family:var(--mono);font-size:10px;
  display:flex;justify-content:space-between}

.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 14px;margin:12px 0 0;
  font-size:13px}
.kv dt{font-family:var(--mono);font-size:11px;color:var(--faint);
  letter-spacing:.06em;padding-top:2px}
.kv dd{margin:0}

.frame img{width:100%;display:block;border-radius:7px;border:1px solid var(--line)}
.strip{display:flex;gap:7px;overflow-x:auto;padding:2px 0 6px;margin-top:12px}
.strip figure{margin:0;flex:0 0 auto;text-align:center}
.strip img{width:76px;height:76px;display:block;border-radius:5px;
  border:1px solid var(--line);image-rendering:pixelated}
.strip figcaption{font-family:var(--mono);font-size:9.5px;color:var(--faint);
  margin-top:3px;font-variant-numeric:tabular-nums}
.strip .rej img{border-color:var(--bad);opacity:.4}
.strip .best img{border-color:var(--ok);box-shadow:0 0 0 1px var(--ok)}

.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:11.5px}
th{text-align:left;font-weight:500;color:var(--faint);padding:4px 7px;
  border-bottom:1px solid var(--line);white-space:nowrap;letter-spacing:.06em}
td{padding:3px 7px;border-bottom:1px solid #ffffff08;vertical-align:top}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
td.m{color:var(--ok)}
tr.st-error td.m{color:var(--bad)} tr.st-pending td.m{color:var(--warn)}
tr.st-note td.m{color:#c0caf5}
.dev{color:var(--h)}
td.wrap{max-width:270px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.tab{display:flex;gap:6px;align-items:center;background:var(--card2);
  color:var(--h);border:1px solid var(--line);border-radius:6px;
  padding:4px 10px;font-family:var(--mono);font-size:11px;cursor:pointer}
.tab i{font-style:normal;color:var(--faint)}
.tab .dot{width:7px;height:7px;border-radius:50%;background:currentColor}
.tab.on{border-color:var(--h)}
.tab:focus-visible,.tab:hover{border-color:var(--h);outline:none}
.pane{display:none} .pane.on{display:block}
.fold{font-family:var(--mono);font-size:11px;color:var(--faint);margin:0 0 8px}
.evs{max-height:340px;overflow:auto;font-family:var(--mono);font-size:11.5px}
.ev{border-left:2px solid var(--h);padding:1px 0 1px 9px;margin-bottom:2px}
.ev b{color:var(--h);font-weight:600;margin:0 6px}
.ev .d{color:var(--dim)}
.ev.quiet{opacity:.42}
footer{margin-top:24px;color:var(--faint);font-size:12px;
  border-top:1px solid var(--line);padding-top:14px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;
  animation:none!important}}
"""

JS = """
// Radar cycles; everything else on this page is inert by design.
(function(){
  var sets=[].slice.call(document.querySelectorAll('#radar .frameset'));
  if(sets.length){
    var k=0, t=document.getElementById('rt'), kind=document.getElementById('rk');
    var reduce=window.matchMedia('(prefers-reduced-motion:reduce)').matches;
    function show(){
      sets.forEach(function(s,i){s.classList.toggle('on',i===k);});
      var s=sets[k];
      if(t) t.textContent=s.dataset.t||'';
      if(kind){kind.textContent=s.dataset.kind;
        kind.style.color=s.dataset.kind==='forecast'?'var(--warn)':'var(--faint)';}
      k=(k+1)%sets.length;
    }
    show();
    if(!reduce && sets.length>1) setInterval(show,650);
  }
  document.querySelectorAll('.tab').forEach(function(b){
    b.addEventListener('click',function(){
      var d=b.dataset.d;
      document.querySelectorAll('.tab').forEach(function(x){
        x.classList.toggle('on',x===b);});
      document.querySelectorAll('.pane').forEach(function(p){
        p.classList.toggle('on',p.dataset.d===d);});
    });
  });
})();
"""


def build(state, frozen):
    return f"""<meta charset="utf-8">
<title>Balcony Rig Snapshot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;600&display=swap">
<style>{CSS}</style>
<div class="wrapper">
  <h1>ASICAP · balcony rig</h1>
  {head(state, frozen)}
  <div class="grid">
    {sky(state.get('weather'))}
    {focus(state.get('focus'))}
    {preview(state.get('preview'))}
    {commands(state.get('commands') or [])}
    {feedback(state)}
  </div>
  <footer>
    Captured from the live dashboard at <code>{E(SRC)}</code> on
    {E(frozen.strftime('%Y-%m-%d %H:%M:%S'))} local time.
    Weather from Open-Meteo, radar from RainViewer, site 55.69&nbsp;N 12.56&nbsp;E.
    Rig figures in this capture come from the offline test double, not the
    telescope — see <code>fake_air.py</code>.
  </footer>
</div>
<script>{JS}</script>
"""


SRC = DEFAULT_SRC


def main():
    global SRC
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--from", dest="src", default=DEFAULT_SRC,
                   help="running dashboard.py to capture (default %s)" % DEFAULT_SRC)
    p.add_argument("--out", default=DEFAULT_OUT)
    add_log_args(p)
    a = p.parse_args()
    configure_logging(a)
    SRC = a.src.rstrip("/")

    with log.slow("fetching state from %s" % SRC):
        body, _ = fetch(SRC + "/api/state", timeout=60)
    state = json.loads(body)
    frozen = datetime.datetime.now()

    with log.slow("inlining assets", detail=lambda: "%d fetched" % len(_cache)):
        page = build(state, frozen)

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(page)
    kb = os.path.getsize(a.out) / 1024
    ok = sum(1 for v in _cache.values() if v)
    log.info("wrote %s (%.0f kB, %d/%d assets inlined)", a.out, kb, ok, len(_cache))
    print(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
