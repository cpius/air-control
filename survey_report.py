#!/usr/bin/env python3
"""Render the running horizon survey as a single self-contained HTML page.

Re-run after every shot; it rebuilds from the JSONL, so it is always current.
The picture is an altitude-vs-azimuth panorama: sky above the roofline, masonry
below it, which is the shape the balcony actually has.
"""
import json, math, os, sys, time

JSONL = os.path.expanduser(os.environ.get("SKYSURVEY_OUT", "~/ASICAP/skysurvey-east.jsonl"))
HTML = os.path.expanduser(os.environ.get("SKYSURVEY_HTML", "~/ASICAP/sky-survey.html"))
_stem = os.path.basename(JSONL).replace("skysurvey", "").replace(".jsonl", "").strip("-_")
TITLE = (_stem.replace("-", " ").title() + " Balcony Sky Survey") if _stem else "Sky Survey"

AZ0, AZ1 = 330, 280         # fallback span; wraps through north (330->360/0->280)
# The plotted window is derived from the data (see fit_span) -- an east-balcony
# window silently drops every west-balcony point, which on 2026-08-26 rendered a
# whole survey as an empty panorama.
ALT0, ALT1 = 0, 90
W, H = 1180, 420
PADL, PADR, PADT, PADB = 58, 22, 22, 46


def load(path):
    recs = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try: recs.append(json.loads(line))
                    except Exception: pass
    return recs


def compass(az):
    pts = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S",
           "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return pts[int((az % 360) / 22.5 + 0.5) % 16]


SPAN = (AZ1 - AZ0) % 360


def fit_span(recs, pad=12.0, minimum=90.0):
    """Set the panorama window to the arc the data actually occupies.

    Azimuth is circular, so the widest EMPTY gap is what to cut out: the plot
    spans everything else. Falls back to the module defaults with no data."""
    global AZ0, AZ1, SPAN
    az = sorted({(r.get("true_az", r["want_az"]) if r.get("solved") else r["want_az"]) % 360
                 for r in recs if r.get("verdict") in ("OPEN", "BLOCKED")})
    if len(az) < 2:
        return
    gaps = [((az[(i + 1) % len(az)] - a) % 360, i) for i, a in enumerate(az)]
    widest, i = max(gaps)
    if widest <= pad * 2:                       # data rings the whole sky
        AZ0, SPAN = 0.0, 360.0
    else:
        start = (az[(i + 1) % len(az)] - pad) % 360
        SPAN = max(minimum, 360.0 - widest + 2 * pad)
        # Snap the window to whole 5-degree steps so the axis reads 250 260 270
        # rather than 253.4 268.4 283.4. Widen, never narrow: the snap must not
        # push a measured point outside the plot.
        AZ0 = math.floor(start / 5.0) * 5.0
        SPAN = math.ceil((SPAN + (start - AZ0)) / 5.0) * 5.0
        SPAN = min(SPAN, 360.0)
    AZ1 = (AZ0 + SPAN) % 360


def x_of(az):
    """Azimuth -> x, wrapping through north so due N sits inside the plot."""
    return PADL + ((az - AZ0) % 360) / SPAN * (W - PADL - PADR)


def in_span(az):
    return ((az - AZ0) % 360) <= SPAN
def y_of(alt): return H - PADB - (alt - ALT0) / (ALT1 - ALT0) * (H - PADT - PADB)


def roofline(recs):
    """Per azimuth: lowest OPEN altitude and highest BLOCKED altitude.
    The roof sits between them; the gap is how well we have pinned it."""
    by = {}
    for r in recs:
        if r.get("verdict") not in ("OPEN", "BLOCKED"):
            continue
        # Bucket by the COMMANDED azimuth always. Using true_az for solved shots
        # and want_az for blocked ones splits one grid azimuth into two buckets
        # (300 and 301), and each half then looks unbracketed. Plotting still
        # uses true_az -- this is only about which grid column a shot belongs to.
        az = round(r["want_az"])
        d = by.setdefault(az, {"open": [], "blocked": []})
        alt = r.get("true_alt", r["want_alt"]) if r.get("solved") else r["want_alt"]
        d["open" if r["verdict"] == "OPEN" else "blocked"].append(alt)
    out = {}
    for az, d in sorted(by.items()):
        lo = min(d["open"]) if d["open"] else None
        hi = max(d["blocked"]) if d["blocked"] else None
        out[az] = (lo, hi)
    return out


def svg(recs):
    p = []
    p.append(f'<svg viewBox="0 0 {W} {H}" class="pan" xmlns="http://www.w3.org/2000/svg">')
    p.append(f'<rect x="{PADL}" y="{PADT}" width="{W-PADL-PADR}" height="{H-PADT-PADB}" '
             f'fill="var(--sky)" stroke="var(--grid)"/>')
    # grid
    for alt in range(0, 91, 15):
        y = y_of(alt)
        p.append(f'<line x1="{PADL}" y1="{y:.1f}" x2="{W-PADR}" y2="{y:.1f}" stroke="var(--grid)" stroke-dasharray="2 4"/>')
        p.append(f'<text x="{PADL-9}" y="{y+4:.1f}" class="ax" text-anchor="end">{alt}°</text>')
    for az in [(AZ0 + k) % 360 for k in range(0, int(SPAN) + 1, 15)]:
        x = x_of(az)
        p.append(f'<line x1="{x:.1f}" y1="{PADT}" x2="{x:.1f}" y2="{H-PADB}" stroke="var(--grid)" stroke-dasharray="2 4"/>')
        p.append(f'<text x="{x:.1f}" y="{H-PADB+16}" class="ax" text-anchor="middle">{az:.0f}°</text>')
        p.append(f'<text x="{x:.1f}" y="{H-PADB+31}" class="ax dim" text-anchor="middle">{compass(az)}</text>')

    # masonry band: fill under the confirmed roofline
    rl = roofline(recs)
    known = sorted(((az, v) for az, v in rl.items() if v[1] is not None),
                   key=lambda t: (t[0] - AZ0) % 360)
    if known:
        pts = " ".join(f"{x_of(az):.1f},{y_of(v[1]):.1f}" for az, v in known)
        first, last = known[0][0], known[-1][0]
        p.append(f'<polygon points="{x_of(first):.1f},{y_of(0):.1f} {pts} {x_of(last):.1f},{y_of(0):.1f}" '
                 f'fill="var(--wall)" opacity="0.55"/>')

    # points -- every record is drawn. A shot that is skipped here is a shot
    # that looks like it was never taken, which is how an east-tuned azimuth
    # window hid an entire west survey on 2026-08-26.
    #
    # Every marker is a <g> laid out as: <title> FIRST, then the visible shape,
    # then an invisible fat circle. Both parts matter for the tooltip:
    #   * a <title> that is not the first child is ignored by Chrome, which is
    #     why the BLOCKED crosses never showed one;
    #   * a cross is two 2px strokes with no interior, so even a valid title is
    #     unhittable -- the transparent circle gives it a real hover target.
    def marker(x, y, cls, shape, tip):
        p.append(f'<g class="{cls}"><title>{tip}</title>{shape}'
                 f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="10"/></g>')

    for r in recs:
        verdict = r.get("verdict")
        solved = r.get("solved")
        stamp = f'{r.get("exp")}s exposure\n{r.get("t","")}'
        if verdict not in ("OPEN", "BLOCKED"):
            az, alt = r.get("want_az"), r.get("want_alt")
            if az is None or alt is None or not in_span(az):
                continue
            x, y = x_of(az), y_of(alt)
            marker(x, y, "oth", f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5"/>',
                   f'{verdict}  az {az:.1f}\u00b0 alt {alt:.1f}\u00b0\n'
                   f'{r.get("error") or r.get("note") or ""}\n{stamp}')
            continue
        az = r.get("true_az", r["want_az"]) if solved else r["want_az"]
        alt = r.get("true_alt", r["want_alt"]) if solved else r["want_alt"]
        if not in_span(az):
            continue
        x, y = x_of(az), y_of(alt)
        if solved:
            n = r.get("stars") or 0
            rad = 3.2 + min(7.0, math.sqrt(n) / 3.4)
            marker(x, y, "open-g",
                   f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{rad:.1f}" class="open"/>',
                   f'OPEN  az {az:.2f}\u00b0 alt {alt:.2f}\u00b0\n{n} stars\n'
                   f'solve {r.get("solve_s")}s\n{stamp}')
        else:
            marker(x, y, "blk",
                   f'<line x1="{x-4.5:.1f}" y1="{y-4.5:.1f}" x2="{x+4.5:.1f}" y2="{y+4.5:.1f}"/>'
                   f'<line x1="{x-4.5:.1f}" y1="{y+4.5:.1f}" x2="{x+4.5:.1f}" y2="{y-4.5:.1f}"/>',
                   f'BLOCKED  az {az:.2f}\u00b0 alt {alt:.2f}\u00b0\n'
                   f'no solve, gave up at {r.get("solve_s")}s\n{stamp}')
    p.append(f'<text x="{PADL-42}" y="{PADT+ (H-PADT-PADB)/2}" class="ax dim" '
             f'transform="rotate(-90 {PADL-42} {PADT+(H-PADT-PADB)/2})" text-anchor="middle">altitude</text>')
    p.append("</svg>")
    return "\n".join(p)


def table(recs):
    rows = []
    for r in reversed(recs):
        v = r.get("verdict", "?")
        cls = {"OPEN": "ok", "BLOCKED": "no", "ERROR": "er", "VOID": "er", "AMBIGUOUS": "er"}.get(v, "")
        if r.get("solved"):
            true = f'{r["true_az"]:.2f}° / {r["true_alt"]:.2f}°'
            stars = f'{r["stars"]}'
            off = f'{r.get("off_az", 0):+.2f} / {r.get("off_alt", 0):+.2f}'
        else:
            true = "—"
            stars = "0"
            off = "—"
        rows.append(
            f'<tr class="{cls}"><td class="mono">{r.get("t","")[11:]}</td>'
            f'<td class="mono">{r["want_az"]:.1f}°</td><td class="mono">{r["want_alt"]:.1f}°</td>'
            f'<td><span class="pill {cls}">{v}</span></td>'
            f'<td class="mono">{true}</td><td class="mono num">{stars}</td>'
            f'<td class="mono num">{r.get("solve_s","—")}</td>'
            f'<td class="mono num">{r.get("exp","—")}</td>'
            f'<td class="mono dim">{off}</td>'
            f'<td class="dim">{r.get("error") or r.get("note") or r.get("label") or ""}</td></tr>')
    return "\n".join(rows)


TOGGLE_JS = """
<script>
(function () {
  var cb = document.getElementById('dots'), svg = document.querySelector('svg.pan');
  if (!cb || !svg) return;
  function apply() {
    svg.classList.toggle('nodots', !cb.checked);
    try { localStorage.setItem('skysurvey-dots', cb.checked ? '1' : '0'); } catch (e) {}
  }
  try { if (localStorage.getItem('skysurvey-dots') === '0') cb.checked = false; } catch (e) {}
  apply();
  cb.addEventListener('change', apply);
})();
</script>
"""


def render():
    recs = load(JSONL)
    fit_span(recs)
    done = [r for r in recs if r.get("verdict") in ("OPEN", "BLOCKED")]
    nopen = sum(1 for r in done if r["verdict"] == "OPEN")
    nblk = len(done) - nopen
    solves = [r["solve_s"] for r in done if r.get("solved")]
    med = sorted(solves)[len(solves) // 2] if solves else 0
    stars = [r["stars"] for r in done if r.get("solved")]
    rl = roofline(recs)
    pinned = [(az, v) for az, v in rl.items() if v[0] is not None and v[1] is not None]

    cards = [
        ("shots", str(len(done)), "plate solves attempted"),
        ("open sky", str(nopen), "solved — sky confirmed"),
        ("blocked", str(nblk), "no solve — masonry"),
        ("median solve", f"{med:.0f}s", f"{min(solves):.0f}–{max(solves):.0f}s range" if solves else "—"),
        ("stars", f"{max(stars) if stars else 0}", f"best frame; median {sorted(stars)[len(stars)//2] if stars else 0}"),
        ("roofline pinned", str(len(pinned)), "azimuths bracketed open+blocked"),
        ("records", f"{len(recs)}", f"{len(recs)-len(done)} not open/blocked" if len(recs) != len(done) else "all open/blocked"),
    ]
    cardhtml = "\n".join(
        f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div><div class="s">{s}</div></div>'
        for k, v, s in cards)

    rows = []
    for az, (lo, hi) in sorted(rl.items()):
        if lo is None and hi is None:
            continue
        if lo is not None and hi is not None:
            verdict = f"roof between {hi:.1f}° and {lo:.1f}°"
            gap = f"±{(lo-hi)/2:.1f}°"
        elif lo is not None:
            verdict = f"open at {lo:.1f}°, roof not yet found below"
            gap = "unbounded below"
        else:
            verdict = f"blocked to {hi:.1f}°, no sky found yet"
            gap = "unbounded above"
        rows.append(f'<tr><td class="mono">{az}°</td><td class="dim">{compass(az)}</td>'
                    f'<td>{verdict}</td><td class="mono dim">{gap}</td></tr>')
    roofrows = "\n".join(rows) or '<tr><td colspan="4" class="dim">nothing bracketed yet</td></tr>'

    html = f"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{TITLE}</title>
<style>
:root {{
  --bg:#fbfaf8; --fg:#1c1b19; --dim:#6b6862; --line:#e2ded7; --card:#ffffff;
  --sky:#eef3f8; --grid:#d3dbe4; --wall:#8a7f72;
  --ok:#1f7a4d; --okbg:#e3f3ea; --no:#a8362b; --nobg:#fbe7e4; --er:#8a6d1f; --erbg:#f7efd9;
}}
:root:not([data-theme="light"]) {{}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg:#16181c; --fg:#e8e6e2; --dim:#9a978f; --line:#2b2f36; --card:#1d2026;
    --sky:#1b222c; --grid:#2e3742; --wall:#5b5348;
    --ok:#5fd39a; --okbg:#14301f; --no:#f08a7d; --nobg:#331815; --er:#e0be6a; --erbg:#2e2612;
  }}
}}
:root[data-theme="dark"] {{
  --bg:#16181c; --fg:#e8e6e2; --dim:#9a978f; --line:#2b2f36; --card:#1d2026;
  --sky:#1b222c; --grid:#2e3742; --wall:#5b5348;
  --ok:#5fd39a; --okbg:#14301f; --no:#f08a7d; --nobg:#331815; --er:#e0be6a; --erbg:#2e2612;
}}
* {{ box-sizing:border-box; }}
body {{ background:var(--bg); color:var(--fg); margin:0; padding:28px 22px 60px;
  font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif; }}
.wrap {{ max-width:1240px; margin:0 auto; }}
h1 {{ font-size:26px; margin:0 0 4px; letter-spacing:-0.01em; }}
.sub {{ color:var(--dim); margin:0 0 22px; font-size:14px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-bottom:24px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }}
.card .k {{ font-size:11px; text-transform:uppercase; letter-spacing:.07em; color:var(--dim); }}
.card .v {{ font-size:25px; font-weight:600; margin:2px 0 1px; font-variant-numeric:tabular-nums; }}
.card .s {{ font-size:12px; color:var(--dim); }}
.panel {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:16px; margin-bottom:24px; overflow-x:auto; }}
h2 {{ font-size:16px; margin:0 0 12px; }}
svg.pan {{ display:block; width:100%; min-width:900px; height:auto; }}
.ax {{ font-size:10.5px; fill:var(--dim); font-family:ui-monospace,Menlo,monospace; }}
.ax.dim {{ opacity:.6; }}
circle.open {{ fill:var(--ok); fill-opacity:.7; stroke:var(--ok); }}
g.blk line {{ stroke:var(--no); stroke-width:2.1; stroke-linecap:round; }}
g.oth circle {{ fill:none; stroke:var(--er); stroke-width:1.8; stroke-dasharray:2.6 2.2; }}
g.oth circle.hit, circle.hit {{ fill:transparent; stroke:none; }}
svg.pan.nodots g.open-g, svg.pan.nodots g.blk, svg.pan.nodots g.oth {{ display:none; }}
.toggle {{ display:inline-flex; align-items:center; gap:6px; font-size:12.5px; color:var(--dim);
  cursor:pointer; user-select:none; margin-left:auto; }}
.toggle input {{ cursor:pointer; margin:0; }}
svg.pan g[class] {{ pointer-events:all; cursor:crosshair; }}
.legend {{ display:flex; gap:18px; flex-wrap:wrap; font-size:12.5px; color:var(--dim); margin-top:10px; }}
.legend i {{ display:inline-block; width:11px; height:11px; border-radius:50%; margin-right:5px; vertical-align:-1px; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th {{ text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--dim); border-bottom:1px solid var(--line); padding:7px 9px; font-weight:600; white-space:nowrap; }}
td {{ padding:6px 9px; border-bottom:1px solid var(--line); }}
.mono {{ font-family:ui-monospace,Menlo,monospace; font-size:12.5px; white-space:nowrap; }}
.num {{ text-align:right; }}
.dim {{ color:var(--dim); }}
.pill {{ display:inline-block; padding:1px 8px; border-radius:20px; font-size:11px; font-weight:600; }}
.pill.ok {{ background:var(--okbg); color:var(--ok); }}
.pill.no {{ background:var(--nobg); color:var(--no); }}
.pill.er {{ background:var(--erbg); color:var(--er); }}
.scroll {{ max-height:520px; overflow-y:auto; }}
</style>
<div class="wrap">
<h1>{TITLE}</h1>
<p class="sub">Copenhagen 55.689°N 12.555°E · C8 + ASI585MC Air, 30.3′ × 17.0′ field ·
Each point is one exposure plate-solved on the Air. A solve means real sky; a failed
solve at the same exposure means masonry. Updated {time.strftime('%Y-%m-%d %H:%M:%S')}.</p>

<div class="cards">{cardhtml}</div>

<div class="panel">
<h2>Panorama — what the balcony can see</h2>
{svg(recs)}
<div class="legend">
  <label class="toggle"><input type="checkbox" id="dots" checked> show measurements</label>
  <span><i style="background:var(--ok)"></i>solved — open sky (dot size ∝ √stars)</span>
  <span><i style="background:var(--no);border-radius:0"></i>✕ no solve — blocked</span>
  <span><i style="background:var(--wall)"></i>masonry, below the confirmed roofline</span>
  <span><i style="border:1.8px dashed var(--er)"></i>error / ambiguous — shot taken, no verdict</span>
{TOGGLE_JS}
  <span>hover any marker for detail</span>
</div>
</div>

<div class="panel">
<h2>Roofline by azimuth</h2>
<table><thead><tr><th>azimuth</th><th></th><th>state</th><th>precision</th></tr></thead>
<tbody>{roofrows}</tbody></table>
</div>

<div class="panel">
<h2>Every shot, newest first</h2>
<div class="scroll">
<table><thead><tr><th>time</th><th>az</th><th>alt</th><th>verdict</th>
<th>true az / alt</th><th>stars</th><th>solve s</th><th>exp s</th>
<th>reg offset az/alt</th><th>note</th></tr></thead>
<tbody>{table(recs)}</tbody></table>
</div>
</div>
</div>
"""
    with open(HTML, "w") as f:
        f.write(html)
    return HTML, len(done)


if __name__ == "__main__":
    path, n = render()
    print(f"{path}  ({n} shots)")
