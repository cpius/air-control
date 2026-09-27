#!/usr/bin/env python3
"""Sky conditions for the balcony: what it is doing now, and what is 30 minutes out.

Three questions, three sources, no API keys and no third-party packages:

    is it clear NOW          Open-Meteo `current` cloud cover
    will it stay clear       Open-Meteo `minutely_15`, 15-minute steps out to 12 h
    is rain arriving         RainViewer radar, ~10 past frames + 2 nowcast frames

Open-Meteo is free for non-commercial use and needs no registration, which is
the only reason it is the default over DMI -- DMI's open data is better resolved
for Denmark but wants an account and an API key per service. Pass --dmi-key to
prefer it once you have one; without a key the Open-Meteo path is complete on
its own.

Two derived numbers earn their place next to the raw cloud cover:

`dew_spread` -- ambient temperature minus dew point, in Kelvin. This is the
number that decides whether the 6.2 W dew heater is worth its share of the
battery. Below ~2 K the corrector plate is going to fog; above ~5 K it will not,
and the heater is 6.2 W of a ~42 W budget spent on nothing. It matters more here
than cloud cover does, because dew ends a session permanently while a cloud bank
merely interrupts one.

`darkness` -- sun altitude, and which twilight that puts us in. Astronomical
darkness never arrives at 55.7 N between roughly mid-May and late July, so
"astronomical" vs "nautical" is a real planning constraint for a Copenhagen
summer rather than a formality.

Moon altitude and illuminated fraction come from the same low-precision
ephemeris. They are good to a fraction of a degree, which is far beyond what
"is the Moon up and how bad is it" requires.

    python3 dashboard/weather.py                      # human-readable, current conditions
    python3 dashboard/weather.py --json               # the full state dict, for the dashboard
    python3 dashboard/weather.py --radar              # add RainViewer frame list + tile URLs
    python3 dashboard/weather.py --watch 300          # re-poll every 5 minutes
"""

import argparse
import datetime
import json
import math
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

from airlog import add_log_args, configure_logging, get_logger

log = get_logger("weather")

# The balcony. See the observing-site note: the 55.96/10.55 that turns up in
# FITS headers is a stale ASIAIR setting, not where the photons were collected.
LAT, LON = 55.69, 12.56
TZ = "Europe/Copenhagen"

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
RAINVIEWER = "https://api.rainviewer.com/public/weather-maps.json"
RAINVIEWER_TILES = "https://tilecache.rainviewer.com"

# Dew point spread, in Kelvin, below which the corrector plate is at risk.
# Glass radiates to a cold sky and sits BELOW ambient -- typically 1-3 K below
# on a clear night -- so dew forms while the air itself is still short of
# saturation. A threshold of 0 would fire far too late.
DEW_WARN_K = 5.0
DEW_CRIT_K = 2.5

TIMEOUT = 15


def _get(url, timeout=TIMEOUT):
    log.debug("GET %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": "asicap-dashboard/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# --------------------------------------------------------------------------
# Sun and Moon. Low-precision formulae from the Astronomical Almanac; good to
# ~0.01 deg for the Sun and ~0.3 deg for the Moon, which is orders of magnitude
# better than any decision made from them here.
# --------------------------------------------------------------------------

def _julian(dt):
    """Julian date from an aware or naive-UTC datetime."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    a = (14 - dt.month) // 12
    y, m = dt.year + 4800 - a, dt.month + 12 * a - 3
    jdn = (dt.day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400
           - 32045)
    frac = (dt.hour - 12) / 24.0 + dt.minute / 1440.0 + dt.second / 86400.0
    return jdn + frac


def _gmst(jd):
    """Greenwich mean sidereal time in degrees."""
    return (280.46061837 + 360.98564736629 * (jd - 2451545.0)) % 360.0


def _altaz(ra_deg, dec_deg, jd, lat=LAT, lon=LON):
    """Equatorial to horizontal. Returns (altitude, azimuth) in degrees."""
    lst = (_gmst(jd) + lon) % 360.0
    ha = math.radians((lst - ra_deg) % 360.0)
    dec, la = math.radians(dec_deg), math.radians(lat)
    sin_alt = math.sin(dec) * math.sin(la) + math.cos(dec) * math.cos(la) * math.cos(ha)
    alt = math.asin(max(-1.0, min(1.0, sin_alt)))
    az = math.atan2(-math.sin(ha) * math.cos(dec),
                    math.cos(la) * math.sin(dec) - math.sin(la) * math.cos(dec) * math.cos(ha))
    return math.degrees(alt), math.degrees(az) % 360.0


def sun(dt=None):
    dt = dt or datetime.datetime.now(datetime.timezone.utc)
    jd = _julian(dt)
    n = jd - 2451545.0
    L = math.radians((280.460 + 0.9856474 * n) % 360.0)
    g = math.radians((357.528 + 0.9856003 * n) % 360.0)
    lam = L + math.radians(1.915) * math.sin(g) + math.radians(0.020) * math.sin(2 * g)
    eps = math.radians(23.439 - 0.0000004 * n)
    ra = math.degrees(math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))) % 360.0
    dec = math.degrees(math.asin(math.sin(eps) * math.sin(lam)))
    alt, az = _altaz(ra, dec, jd)
    return {"altitude": round(alt, 2), "azimuth": round(az, 2),
            "twilight": _twilight(alt)}


def _twilight(alt):
    """Which darkness regime a solar altitude puts us in.

    The boundary that matters at this latitude is -18: between mid-May and late
    July the Sun never gets there from Copenhagen, so a summer session runs in
    permanent nautical twilight and the sky background never bottoms out.
    """
    if alt > 0:
        return "day"
    if alt > -6:
        return "civil"
    if alt > -12:
        return "nautical"
    if alt > -18:
        return "astronomical"
    return "dark"


def moon(dt=None):
    dt = dt or datetime.datetime.now(datetime.timezone.utc)
    jd = _julian(dt)
    d = jd - 2451545.0
    L = math.radians((218.316 + 13.176396 * d) % 360.0)   # mean longitude
    M = math.radians((134.963 + 13.064993 * d) % 360.0)   # mean anomaly
    F = math.radians((93.272 + 13.229350 * d) % 360.0)    # argument of latitude
    lam = L + math.radians(6.289) * math.sin(M)
    beta = math.radians(5.128) * math.sin(F)
    eps = math.radians(23.439 - 0.0000004 * d)
    ra = math.degrees(math.atan2(
        math.sin(lam) * math.cos(eps) - math.tan(beta) * math.sin(eps),
        math.cos(lam))) % 360.0
    dec = math.degrees(math.asin(
        math.sin(beta) * math.cos(eps) + math.cos(beta) * math.sin(eps) * math.sin(lam)))
    alt, az = _altaz(ra, dec, jd)
    s = sun(dt)
    # Elongation from the Sun gives the illuminated fraction directly; the
    # synodic-age route drifts by a day or more near the quarters.
    sl = math.radians((280.460 + 0.9856474 * d) % 360.0)
    elong = math.acos(math.cos(beta) * math.cos(lam - sl))
    return {"altitude": round(alt, 2), "azimuth": round(az, 2),
            "illumination": round((1 - math.cos(elong)) / 2, 3),
            "up": alt > 0, "_sun_alt": s["altitude"]}


# --------------------------------------------------------------------------
# Open-Meteo
# --------------------------------------------------------------------------

def forecast(lat=LAT, lon=LON, hours=12):
    """Current conditions plus 15-minute steps, which is the 30-minute answer."""
    url = (f"{OPEN_METEO}?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,relative_humidity_2m,dew_point_2m,"
           "cloud_cover,wind_speed_10m,wind_gusts_10m,precipitation,visibility"
           "&minutely_15=cloud_cover,precipitation,visibility,temperature_2m,"
           "dew_point_2m"
           "&hourly=cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high,"
           "temperature_2m,dew_point_2m,wind_speed_10m"
           f"&forecast_days=2&timezone={TZ}")
    d = _get(url)
    cur = d.get("current", {})
    t, dp = cur.get("temperature_2m"), cur.get("dew_point_2m")
    spread = None if t is None or dp is None else round(t - dp, 1)

    # Trim the 15-minute series to a window starting now. Open-Meteo returns
    # the whole day, most of which is already in the past by the time anyone
    # looks at this.
    now = datetime.datetime.now()
    m = d.get("minutely_15", {})
    times = m.get("time", []) or []
    idx = 0
    for i, ts in enumerate(times):
        if datetime.datetime.fromisoformat(ts) >= now:
            idx = max(0, i - 1)
            break
    n = hours * 4
    series = [{"time": times[i],
               "cloud": _at(m, "cloud_cover", i),
               "precip": _at(m, "precipitation", i),
               "visibility": _at(m, "visibility", i),
               "dew_spread": _spread(m, i)}
              for i in range(idx, min(len(times), idx + n))]

    return {
        "source": "open-meteo",
        "fetched": datetime.datetime.now().isoformat(timespec="seconds"),
        "current": {
            "temperature": t,
            "humidity": cur.get("relative_humidity_2m"),
            "dew_point": dp,
            "dew_spread": spread,
            "dew_risk": _dew_risk(spread),
            "cloud_cover": cur.get("cloud_cover"),
            "wind": cur.get("wind_speed_10m"),
            "gusts": cur.get("wind_gusts_10m"),
            "precipitation": cur.get("precipitation"),
            "visibility": cur.get("visibility"),
        },
        "series": series,
        "outlook": _outlook(series),
        "hourly": _hourly(d.get("hourly", {})),
    }


def _at(block, key, i):
    v = block.get(key) or []
    return v[i] if i < len(v) else None


def _spread(block, i):
    t, dp = _at(block, "temperature_2m", i), _at(block, "dew_point_2m", i)
    return None if t is None or dp is None else round(t - dp, 1)


def _dew_risk(spread):
    if spread is None:
        return "unknown"
    if spread <= DEW_CRIT_K:
        return "critical"
    if spread <= DEW_WARN_K:
        return "warning"
    return "clear"


def _hourly(h, n=24):
    times = h.get("time", []) or []
    now = datetime.datetime.now()
    idx = next((max(0, i - 1) for i, ts in enumerate(times)
                if datetime.datetime.fromisoformat(ts) >= now), 0)
    return [{"time": times[i],
             "cloud": _at(h, "cloud_cover", i),
             "low": _at(h, "cloud_cover_low", i),
             "mid": _at(h, "cloud_cover_mid", i),
             "high": _at(h, "cloud_cover_high", i),
             "dew_spread": _spread(h, i)}
            for i in range(idx, min(len(times), idx + n))]


def _outlook(series):
    """The 30-minute question, answered as a sentence and a verdict.

    Deliberately blunt: two 15-minute steps is the horizon over which anyone
    would actually change what they are doing -- abort a sub, close the cover,
    keep going.
    """
    if not series:
        return {"verdict": "unknown", "text": "no forecast series"}
    nxt = series[:3]                       # now, +15, +30
    clouds = [s["cloud"] for s in nxt if s["cloud"] is not None]
    precip = [s["precip"] for s in nxt if s["precip"] is not None]
    if not clouds:
        return {"verdict": "unknown", "text": "no cloud data"}
    now_c, end_c = clouds[0], clouds[-1]
    rain = max(precip) if precip else 0.0
    delta = end_c - now_c

    if rain > 0.1:
        return {"verdict": "rain", "delta": delta,
                "text": f"precipitation within 30 min ({rain:.1f} mm) — cover the rig"}
    if end_c >= 80 and delta > 15:
        return {"verdict": "closing", "delta": delta,
                "text": f"clouding over fast: {now_c:.0f}% → {end_c:.0f}% in 30 min"}
    if delta > 25:
        return {"verdict": "worsening", "delta": delta,
                "text": f"cloud building: {now_c:.0f}% → {end_c:.0f}% in 30 min"}
    if delta < -25:
        return {"verdict": "clearing", "delta": delta,
                "text": f"clearing: {now_c:.0f}% → {end_c:.0f}% in 30 min"}
    if end_c <= 20:
        return {"verdict": "clear", "delta": delta,
                "text": f"holding clear ({end_c:.0f}% in 30 min)"}
    return {"verdict": "steady", "delta": delta,
            "text": f"steady at {end_c:.0f}% cloud over the next 30 min"}


# --------------------------------------------------------------------------
# RainViewer -- the picture behind the number, and the only source here that
# shows which DIRECTION weather is arriving from.
# --------------------------------------------------------------------------

def _tile_xy(lat, lon, z):
    """Fractional slippy-map tile coordinate. The fraction is the point --
    Copenhagen lands at 68.46/40.05 at zoom 7, i.e. hard against the corner of
    tile 68/40, so a single-tile radar view would put the balcony in the
    margin with most of the frame over the Baltic.
    """
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    la = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(la)) / math.pi) / 2.0 * n
    return x, y


# RainViewer's free tier serves radar only to zoom 7 -- z8 and up return an
# identical 1370-byte "Zoom Level Not Supported" placeholder. So the view cannot
# simply be zoomed in: the radar layer stays at 7 and is scaled to fit, while the
# basemap is drawn at a higher zoom where Danish place names are legible.
RADAR_MAX_Z = 7


def _layer(x0f, y0f, x1f, y1f, z, view_z, url_for):
    """Tiles covering a view box, each with its rect as a percentage of the view.

    The box is given in fractional tile units at `view_z`; this converts it to
    `z` and returns whichever tiles overlap, positioned and sized so the layer
    lines up geographically with every other layer regardless of its zoom.
    """
    k = 2.0 ** (z - view_z)
    x0, y0, x1, y1 = x0f * k, y0f * k, x1f * k, y1f * k
    W, H = x1 - x0, y1 - y0
    out = []
    for ty in range(math.floor(y0), math.ceil(y1)):
        for tx in range(math.floor(x0), math.ceil(x1)):
            out.append({
                "url": url_for(z, tx, ty),
                # +0.06 on the extents closes the hairline seams that otherwise
                # show between tiles at fractional pixel positions.
                "left": round((tx - x0) / W * 100, 4),
                "top": round((ty - y0) / H * 100, 4),
                "width": round(1 / W * 100 + 0.06, 4),
                "height": round(1 / H * 100 + 0.06, 4),
            })
    return out


def radar(lat=LAT, lon=LON, view_z=8, span=3, past=8, tile_px=256):
    """Radar and basemap over a box centred on the site, as positioned tiles.

    `view_z` and `span` define the box: 3 tiles at zoom 8 is about 265 km
    across, which is roughly 2.5 hours of warning at ordinary frontal speeds
    and still shows Danish place names. Zoom 7 was the first attempt and was
    wrong -- 530 km across, Copenhagen reduced to a speck, and the largest
    label on the map was Bremen.

    The site lands at exactly 50%/50% because the box is built around it rather
    than snapped to a tile boundary, so the crosshair needs no correction.

    Nowcast frames are RainViewer's own motion extrapolation, usually two at +10
    and +20 minutes and sometimes absent entirely. They are reliable for "this
    band keeps tracking northeast" and useless for anything forming or
    dissipating in place, so the numeric 30-minute answer stays with Open-Meteo
    and this is the picture beside it.
    """
    d = _get(RAINVIEWER)
    host = d.get("host", RAINVIEWER_TILES)
    r = d.get("radar", {})
    fx, fy = _tile_xy(lat, lon, view_z)
    h = span / 2.0
    box = (fx - h, fy - h, fx + h, fy + h)
    rz = min(view_z, RADAR_MAX_Z)

    def frame(f, nowcast):
        ts = f.get("time")
        return {
            "time": ts,
            "iso": datetime.datetime.fromtimestamp(ts).isoformat(timespec="minutes")
                   if ts else None,
            "nowcast": nowcast,
            # colour scheme 2, smoothed, no snow overlay
            "tiles": _layer(*box, rz, view_z, lambda z, x, y:
                            f"{host}{f.get('path')}/{tile_px}/{z}/{x}/{y}/2/1_1.png"),
        }

    frames = [frame(f, False) for f in (r.get("past") or [])[-past:]]
    frames += [frame(f, True) for f in (r.get("nowcast") or [])]

    km = span * 360.0 / (2 ** view_z) * 111.32 * math.cos(math.radians(lat))
    return {
        "frames": frames,
        "basemap": _layer(*box, view_z, view_z, lambda z, x, y:
                          f"https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
        "view_zoom": view_z, "radar_zoom": rz, "span": span,
        "site_pct": [50.0, 50.0], "km_across": round(km),
        "generated": d.get("generated"),
    }


def snapshot(with_radar=True):
    """Everything the dashboard's weather pane needs, in one dict."""
    out = {"site": {"lat": LAT, "lon": LON, "name": "Copenhagen balcony"}}
    try:
        out.update(forecast())
    except Exception as e:
        log.warn("forecast failed: %s", e)
        out["error"] = str(e)
    out["sun"], out["moon"] = sun(), moon()
    if with_radar:
        try:
            out["radar"] = radar()
        except Exception as e:                  # radar is a nice-to-have
            log.warn("radar failed: %s", e)
            out["radar"] = {"error": str(e), "frames": []}
    return out


def _render(s):
    c = s.get("current", {})
    o = s.get("outlook", {})
    L = []
    L.append(f"  cloud     {c.get('cloud_cover')}%   "
             f"vis {(c.get('visibility') or 0) / 1000:.0f} km   "
             f"wind {c.get('wind')} km/h (gust {c.get('gusts')})")
    L.append(f"  temp      {c.get('temperature')} C   dew point {c.get('dew_point')} C")
    L.append(f"  dew       spread {c.get('dew_spread')} K  -> {c.get('dew_risk').upper()}"
             + ("   (heater earns its 6.2 W)"
                if c.get("dew_risk") in ("warning", "critical") else
                "   (heater off saves 6.2 W)"))
    sn, mn = s.get("sun", {}), s.get("moon", {})
    L.append(f"  sun       alt {sn.get('altitude')}  ({sn.get('twilight')})")
    L.append(f"  moon      alt {mn.get('altitude')}  {mn.get('illumination', 0) * 100:.0f}% lit"
             + ("  UP" if mn.get("up") else "  below horizon"))
    L.append("")
    L.append(f"  NEXT 30 MIN: {o.get('text')}   [{o.get('verdict', '').upper()}]")
    rows = s.get("series", [])[:8]
    if rows:
        L.append("")
        L.append("  " + "  ".join(r["time"][11:16] for r in rows))
        L.append("  " + "  ".join(f"{(r['cloud'] or 0):>4.0f}%" for r in rows))
    rad = s.get("radar", {})
    if rad.get("frames"):
        nc = sum(1 for f in rad["frames"] if f["nowcast"])
        L.append("")
        L.append(f"  radar     {len(rad['frames'])} frames "
                 f"({nc} nowcast), tile {rad.get('tile')} z{rad.get('zoom')}")
    return "\n".join(L)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--json", action="store_true", help="emit the full state dict")
    p.add_argument("--radar", action="store_true", help="include RainViewer frames")
    p.add_argument("--watch", type=int, metavar="SEC", help="re-poll every SEC seconds")
    p.add_argument("--out", help="also write the JSON here (for the dashboard)")
    add_log_args(p)
    a = p.parse_args()
    configure_logging(a)

    import time
    while True:
        s = snapshot(with_radar=a.radar or a.json)
        if a.out:
            tmp = a.out + ".tmp"          # atomic, so a reader never sees half a file
            with open(tmp, "w") as f:
                json.dump(s, f, indent=1)
            os.replace(tmp, a.out)
        if a.json:
            print(json.dumps(s, indent=1))
        else:
            print(f"\n{s['site']['name']}  {s.get('fetched', '')}")
            print(_render(s))
        if not a.watch:
            return 0
        time.sleep(a.watch)


if __name__ == "__main__":
    sys.exit(main())
