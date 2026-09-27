#!/usr/bin/env python3
"""Low-precision Moon ephemeris (Meeus ch. 47 truncated, ~1' in position) plus
apparent diameter and RA/Dec rates, for checking the register and predicting
drift when no ephemeris library is installed. Topocentric correction included
(parallax is up to 1 deg for the Moon -- it matters at 9'x5' fields).

    python3 lib/moonephem.py --lat 55.689444 --lon 12.555278            # now
    python3 lib/moonephem.py --utc 2026-09-24T19:30:00
"""
import argparse, datetime as dt, math

D2R = math.pi / 180.0

def jd_from_utc(t):
    return t.timestamp() / 86400.0 + 2440587.5

def moon_geocentric(jd):
    """Geocentric ecliptic lon/lat (deg), distance (km); Meeus 47 main terms."""
    T = (jd - 2451545.0) / 36525.0
    Lp = (218.3164477 + 481267.88123421 * T - 0.0015786 * T**2 + T**3 / 538841 - T**4 / 65194000) % 360
    D = (297.8501921 + 445267.1114034 * T - 0.0018819 * T**2 + T**3 / 545868 - T**4 / 113065000) % 360
    M = (357.5291092 + 35999.0502909 * T - 0.0001536 * T**2 + T**3 / 24490000) % 360
    Mp = (134.9633964 + 477198.8675055 * T + 0.0087414 * T**2 + T**3 / 69699 - T**4 / 14712000) % 360
    F = (93.2720950 + 483202.0175233 * T - 0.0036539 * T**2 - T**3 / 3526000 + T**4 / 863310000) % 360
    A1 = (119.75 + 131.849 * T) % 360; A2 = (53.09 + 479264.290 * T) % 360; A3 = (313.45 + 481266.484 * T) % 360
    E = 1 - 0.002516 * T - 0.0000074 * T**2
    # (D, M, Mp, F, sum_l, sum_r)
    LR = [(0,0,1,0,6288774,-20905355),(2,0,-1,0,1274027,-3699111),(2,0,0,0,658314,-2955968),(0,0,2,0,213618,-569925),
          (0,1,0,0,-185116,48888),(0,0,0,2,-114332,-3149),(2,0,-2,0,58793,246158),(2,-1,-1,0,57066,-152138),
          (2,0,1,0,53322,-170733),(2,-1,0,0,45758,-204586),(0,1,-1,0,-40923,-129620),(1,0,0,0,-34720,108743),
          (0,1,1,0,-30383,104755),(2,0,0,-2,15327,10321),(0,0,1,2,-12528,0),(0,0,1,-2,10980,79661),
          (4,0,-1,0,10675,-34782),(0,0,3,0,10034,-23210),(4,0,-2,0,8548,-21636),(2,1,-1,0,-7888,24208),
          (2,1,0,0,-6766,30824),(1,0,-1,0,-5163,-8379),(1,1,0,0,4987,-16675),(2,-1,1,0,4036,-12831),
          (2,0,2,0,3994,-10445),(4,0,0,0,3861,-11650),(2,0,-3,0,3665,14403),(0,1,-2,0,-2689,-7003),
          (2,0,-1,2,-2602,0),(2,-1,-2,0,2390,10056),(1,0,1,0,-2348,6322),(2,-2,0,0,2236,-9884),
          (0,1,2,0,-2120,5751),(0,2,0,0,-2069,0),(2,-2,-1,0,2048,-4950),(2,0,1,-2,-1773,4130),
          (2,0,0,2,-1595,0),(4,-1,-1,0,1215,-3958),(0,0,2,2,-1110,0),(3,0,-1,0,-892,3258),
          (2,1,1,0,-810,2616),(4,-1,-2,0,759,-1897),(0,2,-1,0,-713,-2117),(2,2,-1,0,-700,2354),
          (2,1,-2,0,691,0),(2,-1,0,-2,596,0),(4,0,1,0,549,-1423),(0,0,4,0,537,-1117),
          (4,-1,0,0,520,-1571),(1,0,-2,0,-487,-1739),(2,1,0,-2,-399,0),(0,0,2,-2,-381,-4421),
          (1,1,1,0,351,0),(3,0,-2,0,-340,0),(4,0,-3,0,330,0),(2,-1,2,0,327,0),(0,2,1,0,-323,1165),
          (1,1,-1,0,299,0),(2,0,3,0,294,0),(2,0,-1,-2,0,8752)]
    B = [(0,0,0,1,5128122),(0,0,1,1,280602),(0,0,1,-1,277693),(2,0,0,-1,173237),(2,0,-1,1,55413),
         (2,0,-1,-1,46271),(2,0,0,1,32573),(0,0,2,1,17198),(2,0,1,-1,9266),(0,0,2,-1,8822),
         (2,-1,0,-1,8216),(2,0,-2,-1,4324),(2,0,1,1,4200),(2,1,0,-1,-3359),(2,-1,-1,1,2463),
         (2,-1,0,1,2211),(2,-1,-1,-1,2065),(0,1,-1,-1,-1870),(4,0,-1,-1,1828),(0,1,0,1,-1794),
         (0,0,0,3,-1749),(0,1,-1,1,-1565),(1,0,0,1,-1491),(0,1,1,1,-1475),(0,1,1,-1,-1410),
         (0,1,0,-1,-1344),(1,0,0,-1,-1335),(0,0,3,1,1107),(4,0,0,-1,1021),(4,0,-1,1,833),
         (0,0,1,-3,777),(4,0,-2,1,671),(2,0,0,-3,607),(2,0,2,-1,596),(2,-1,1,-1,491),
         (2,0,-2,1,-451),(0,0,3,-1,439),(2,0,2,1,422),(2,0,-3,-1,421),(2,1,-1,1,-366),
         (2,1,0,1,-351),(4,0,0,1,331),(2,-1,1,1,315),(2,-2,0,-1,302),(0,0,1,3,-283),
         (2,1,1,-1,-229),(1,1,0,-1,223),(1,1,0,1,223),(0,1,-2,-1,-220),(2,1,-1,-1,-220),
         (1,0,1,1,-185),(2,-1,-2,-1,181),(0,1,2,1,-177),(4,0,-2,-1,176),(4,-1,-1,-1,166),
         (1,0,1,-1,-164),(4,0,1,-1,132),(1,0,-1,-1,-119),(4,-1,0,-1,115),(2,-2,0,1,107)]
    sl = sr = 0.0
    for d, m, mp, f, l, r in LR:
        arg = (d * D + m * M + mp * Mp + f * F) * D2R
        e = E ** abs(m)
        sl += l * e * math.sin(arg); sr += r * e * math.cos(arg)
    sb = 0.0
    for d, m, mp, f, b in B:
        arg = (d * D + m * M + mp * Mp + f * F) * D2R
        sb += b * (E ** abs(m)) * math.sin(arg)
    sl += 3958 * math.sin(A1 * D2R) + 1962 * math.sin((Lp - F) * D2R) + 318 * math.sin(A2 * D2R)
    sb += -2235 * math.sin(Lp * D2R) + 382 * math.sin(A3 * D2R) + 175 * math.sin((A1 - F) * D2R) + 175 * math.sin((A1 + F) * D2R) + 127 * math.sin((Lp - Mp) * D2R) - 115 * math.sin((Lp + Mp) * D2R)
    lon = Lp + sl / 1e6; lat = sb / 1e6; dist = 385000.56 + sr / 1000.0
    # nutation in longitude (dominant term) + true obliquity
    Om = (125.04452 - 1934.136261 * T) % 360
    Ls = (280.4665 + 36000.7698 * T) % 360
    dpsi = (-17.20 * math.sin(Om * D2R) - 1.32 * math.sin(2 * Ls * D2R) - 0.23 * math.sin(2 * Lp * D2R) + 0.21 * math.sin(2 * Om * D2R)) / 3600
    deps = (9.20 * math.cos(Om * D2R) + 0.57 * math.cos(2 * Ls * D2R) + 0.10 * math.cos(2 * Lp * D2R) - 0.09 * math.cos(2 * Om * D2R)) / 3600
    eps = 23.439291 - 0.0130042 * T + deps
    return (lon + dpsi) % 360, lat, dist, eps

def ecl_to_eq(lon, lat, eps):
    lo, la, e = lon * D2R, lat * D2R, eps * D2R
    ra = math.atan2(math.sin(lo) * math.cos(e) - math.tan(la) * math.sin(e), math.cos(lo))
    dec = math.asin(math.sin(la) * math.cos(e) + math.cos(la) * math.sin(e) * math.sin(lo))
    return (ra / D2R) % 360, dec / D2R

def gmst_deg(jd):
    T = (jd - 2451545.0) / 36525.0
    return (280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * T**2 - T**3 / 38710000) % 360

def topocentric(ra, dec, dist_km, jd, lat, lon, height_m=20.0):
    """Apparent topocentric RA/Dec (deg) from geocentric, Meeus ch. 40."""
    lst = (gmst_deg(jd) + lon) % 360
    H = (lst - ra) * D2R
    u = math.atan(0.99664719 * math.tan(lat * D2R))
    rs = 0.99664719 * math.sin(u) + height_m / 6378140 * math.sin(lat * D2R)
    rc = math.cos(u) + height_m / 6378140 * math.cos(lat * D2R)
    spi = math.sin(8.794 / 3600 * D2R) * 6378.14 / (dist_km / 149597870.7) / 149597870.7 * 149597870.7 / 6378.14 * (6378.14 / dist_km) / math.sin(8.794 / 3600 * D2R) * math.sin(8.794 / 3600 * D2R) if False else math.sin(math.asin(6378.14 / dist_km))
    d = dec * D2R
    dra = math.atan2(-rc * spi * math.sin(H), math.cos(d) - rc * spi * math.cos(H))
    ra_t = ra + dra / D2R
    dec_t = math.atan2((math.sin(d) - rs * spi) * math.cos(dra), math.cos(d) - rc * spi * math.cos(H)) / D2R
    return ra_t % 360, dec_t, lst

def altaz(ra, dec, lst, lat):
    H = (lst - ra) * D2R; d = dec * D2R; ph = lat * D2R
    alt = math.asin(math.sin(ph) * math.sin(d) + math.cos(ph) * math.cos(d) * math.cos(H))
    az = math.atan2(math.sin(H), math.cos(H) * math.sin(ph) - math.tan(d) * math.cos(ph))
    return alt / D2R, (az / D2R + 180) % 360

def parallactic(ra, dec, lst, lat):
    """Position angle of the zenith at the object, N through E (deg)."""
    H = (lst - ra) * D2R; d = dec * D2R; ph = lat * D2R
    return math.atan2(math.sin(H), math.tan(ph) * math.cos(d) - math.sin(d) * math.cos(H)) / D2R

def moon(t, lat, lon):
    jd = jd_from_utc(t)
    lo, la, dist, eps = moon_geocentric(jd)
    ra_g, dec_g = ecl_to_eq(lo, la, eps)
    ra, dec, lst = topocentric(ra_g, dec_g, dist, jd, lat, lon)
    alt, az = altaz(ra, dec, lst, lat)
    diam = 2 * math.degrees(math.asin(1737.4 / (dist - 6378.14 * math.sin(math.radians(max(alt, 0))) ))) * 60
    # elongation / phase from the Sun's mean longitude
    T = (jd - 2451545.0) / 36525.0
    Ls = (280.46646 + 36000.76983 * T) % 360; Ms = (357.52911 + 35999.05029 * T) % 360
    sun_lon = (Ls + 1.914602 * math.sin(Ms * D2R) + 0.019993 * math.sin(2 * Ms * D2R)) % 360
    elong = (lo - sun_lon) % 360
    return dict(jd=jd, ra_h=ra / 15, dec=dec, ra_geo_h=ra_g / 15, dec_geo=dec_g, dist_km=dist, alt=alt, az=az, lst_h=lst / 15,
                diam_arcmin=diam, elong=elong, illum=(1 - math.cos(elong * D2R)) / 2, q=parallactic(ra, dec, lst, lat))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, default=55.689444); ap.add_argument("--lon", type=float, default=12.555278)
    ap.add_argument("--utc", default=None, help="ISO time, default now")
    ap.add_argument("--register", default=None, help="mount RA_h,Dec_deg to compare with")
    a = ap.parse_args()
    t = dt.datetime.fromisoformat(a.utc).replace(tzinfo=dt.timezone.utc) if a.utc else dt.datetime.now(dt.timezone.utc)
    m = moon(t, a.lat, a.lon)
    m2 = moon(t + dt.timedelta(minutes=1), a.lat, a.lon)
    dra = ((m2["ra_h"] - m["ra_h"] + 12) % 24 - 12) * 15 * 60 * math.cos(math.radians(m["dec"]))   # arcmin/min on sky
    ddec = (m2["dec"] - m["dec"]) * 60
    print("UTC %s  LST %.4f h" % (t.strftime("%Y-%m-%d %H:%M:%S"), m["lst_h"]))
    print("Moon topocentric apparent: RA %.4f h  Dec %+.4f  (geocentric RA %.4f h Dec %+.4f)" % (m["ra_h"], m["dec"], m["ra_geo_h"], m["dec_geo"]))
    print("alt %.2f  az %.2f  diam %.1f'  dist %.0f km  elong %.1f deg  illum %.0f%%  parallactic angle q %+.1f deg" % (m["alt"], m["az"], m["diam_arcmin"], m["dist_km"], m["elong"], 100 * m["illum"], m["q"]))
    print("motion vs the stars: dRA %+.3f'/min (on sky)  dDec %+.3f'/min  = %.1f\"/s total" % (dra, ddec, math.hypot(dra, ddec) * 60 / 60))
    if a.register:
        rr, rd = [float(v) for v in a.register.split(",")]
        off_ra = ((rr - m["ra_h"] + 12) % 24 - 12) * 15 * 60 * math.cos(math.radians(m["dec"])); off_dec = (rd - m["dec"]) * 60
        print("register minus Moon: dRA %+.1f' (on sky)  dDec %+.1f'  -> %.1f' apart" % (off_ra, off_dec, math.hypot(off_ra, off_dec)))
