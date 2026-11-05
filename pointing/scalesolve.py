#!/usr/bin/env python3
"""Plate-solve a tiny field when the SCALE is known and the position only to a few degrees.

The Air's solver only searches near the mount's reported position, so after the rig is
carried out (pointing off by degrees) it grinds and fails on a 12'x7' f/15.7 field. Siril
needs local catalogues for a near search. This matches star-pair separations (scale known
to ~0.5%) against a Gaia cone: each image pair fixes the rotation, the rest must agree.

2026-10-03: 13/20 stars, 1.8" rms, 3 s -- field 2.6 deg E / 0.6 deg N of the register; then
5/5 on the flipped pier side (rotation +180 deg, as it must be). Needs the .venv (cv2, scipy).

Catalogue (ESA TAP answered in ~60 s; VizieR crawled at ~1 KB/s that night):
    curl -s -o gaia.csv "https://gea.esac.esa.int/tap-server/tap/sync" \
      --data-urlencode REQUEST=doQuery --data-urlencode LANG=ADQL --data-urlencode FORMAT=csv \
      --data-urlencode "QUERY=SELECT ra,dec,phot_g_mean_mag FROM gaiadr3.gaia_source WHERE \
        1=CONTAINS(POINT(ra,dec),CIRCLE(297.69583,8.86832,4.5)) AND phot_g_mean_mag<13.5"

    ~/ASICAP/.venv/bin/python pointing/scalesolve.py --img frames/x_0.png --cat gaia.csv \
        --scale 0.3732 --ra0 297.69583 --dec0 8.86832      # bin-2 preview frame, 10 s g300

Output is ICRS; convert with findstar.jnow() before comparing with the (JNow) register, and
re-solve after a pier flip -- the offset differs per pier side (2026-10-03: +157'/+32' west,
+194'/-49' east). With only 4 stars, fix the rotation from an earlier solve instead.
"""
import argparse, math, sys, time
import numpy as np
import cv2
from scipy import ndimage
from scipy.spatial import cKDTree


def detect(path, nmax=25, nsig=6.0):
    im = cv2.imread(path, cv2.IMREAD_UNCHANGED).astype(np.float64)
    if im.ndim == 3:
        im = im.mean(2)
    bg = ndimage.median_filter(im[::4, ::4], size=25)
    bg = cv2.resize(bg, (im.shape[1], im.shape[0]), interpolation=cv2.INTER_LINEAR)
    d = im - bg
    sig = 1.4826 * np.median(np.abs(d - np.median(d)))
    sm = ndimage.gaussian_filter(d, 2.0)
    smsig = 1.4826 * np.median(np.abs(sm - np.median(sm)))
    lab, n = ndimage.label(sm > nsig * smsig)
    out = []
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        m = lab[sl] == i
        area = int(m.sum())
        if area < 12:                       # hot pixels smooth to tiny blobs
            continue
        w = np.clip(d[sl], 0, None) * m
        f = w.sum()
        if f <= 0:
            continue
        ys, xs = np.mgrid[sl[0], sl[1]]
        cy, cx = (w * ys).sum() / f, (w * xs).sum() / f
        # reject single-pixel spikes: peak pixel holding most of the flux
        if w.max() / f > 0.5:
            continue
        out.append((cx, cy, f, area))
    out.sort(key=lambda s: -s[2])
    return np.array(out[:nmax]), im.shape, sig


def project(ra, dec, ra0, dec0):
    """Gnomonic projection, arcsec; xi east, eta north."""
    ra, dec, ra0, dec0 = map(np.radians, (ra, dec, ra0, dec0))
    c = np.sin(dec0) * np.sin(dec) + np.cos(dec0) * np.cos(dec) * np.cos(ra - ra0)
    xi = np.cos(dec) * np.sin(ra - ra0) / c
    eta = (np.cos(dec0) * np.sin(dec) - np.sin(dec0) * np.cos(dec) * np.cos(ra - ra0)) / c
    k = 180 / math.pi * 3600
    return xi * k, eta * k


def deproject(xi, eta, ra0, dec0):
    k = 180 / math.pi * 3600
    xi, eta = xi / k, eta / k
    ra0r, dec0r = math.radians(ra0), math.radians(dec0)
    rho = math.hypot(xi, eta)
    c = math.atan(rho)
    if rho == 0:
        return ra0, dec0
    dec = math.asin(math.cos(c) * math.sin(dec0r) + eta * math.sin(c) * math.cos(dec0r) / rho)
    ra = ra0r + math.atan2(xi * math.sin(c), rho * math.cos(dec0r) * math.cos(c) - eta * math.sin(dec0r) * math.sin(c))
    return math.degrees(ra) % 360, math.degrees(dec)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--img", required=True)
    ap.add_argument("--cat", required=True)
    ap.add_argument("--scale", type=float, required=True, help="arcsec per pixel of --img")
    ap.add_argument("--ra0", type=float, required=True, help="search centre, ICRS deg")
    ap.add_argument("--dec0", type=float, required=True)
    ap.add_argument("--tol", type=float, default=4.0, help="match tolerance, arcsec")
    ap.add_argument("--nbright", type=int, default=6, help="image stars used to seed pairs")
    ap.add_argument("--rot", type=float, default=None,
                    help="known rotation (deg, from an earlier solve on the SAME pier side): anchor the brightest "
                         "star on every catalogue star at rot +-4 deg; makes 3-4 star frames decisive")
    ap.add_argument("--parity", type=int, default=1)
    a = ap.parse_args()
    t0 = time.time()

    stars, shape, sig = detect(a.img)
    print("detected %d stars (sigma %.1f) in %s" % (len(stars), sig, a.img))
    for s in stars[:12]:
        print("   x %7.1f y %7.1f flux %9.0f area %d" % tuple(s))
    if len(stars) < 4:
        sys.exit("too few stars")
    H, W = shape
    P = (stars[:, :2] - [W / 2, H / 2]) * a.scale          # image arcsec, y down

    cat = np.genfromtxt(a.cat, delimiter=",", names=True)
    xi, eta = project(cat["ra"], cat["dec"], a.ra0, a.dec0)
    C = np.column_stack([xi, eta])
    tree = cKDTree(C)
    print("catalogue: %d stars" % len(C))

    if a.rot is not None:
        Q = P.copy(); Q[:, 1] *= a.parity
        hits = []
        for rd in np.arange(-4, 4.01, 0.5):
            ang = math.radians(a.rot + rd)
            R = np.array([[math.cos(ang), -math.sin(ang)], [math.sin(ang), math.cos(ang)]])
            T0 = (Q - Q[0]) @ R.T
            for A in range(len(C)):
                dist, idx = tree.query(T0 + C[A], distance_upper_bound=a.tol)
                n = int(np.isfinite(dist).sum())
                if n >= min(3, len(P)):
                    hits.append((n, -float(np.sqrt((dist[np.isfinite(dist)] ** 2).mean())), A, ang, R))
        hits.sort(reverse=True)
        print("fixed-rotation hypotheses with >=3 matches: %d" % len(hits))
        if not hits:
            sys.exit("NO SOLUTION")
        for n, nrms, A, ang, R in hits[:5]:
            c = (np.zeros(2) - Q[0]) @ R.T + C[A]
            ra_c, dec_c = deproject(c[0], c[1], a.ra0, a.dec0)
            print("   %d/%d rms %.2f\" rot %.1f: centre RA %.5f deg (%.5f h) Dec %+.5f" % (n, len(P), -nrms, math.degrees(ang) % 360, ra_c, ra_c / 15, dec_c))
        n, nrms, A, ang, R = hits[0]
        c = (np.zeros(2) - Q[0]) @ R.T + C[A]
        ra_c, dec_c = deproject(c[0], c[1], a.ra0, a.dec0)
        unique = len(hits) == 1 or hits[1][0] < n
        print("%s: centre RA %.5f deg (%.5f h) Dec %+.5f ; %d/%d matched, rms %.2f\"" % (
            "SOLVED" if unique else "AMBIGUOUS", ra_c, ra_c / 15, dec_c, n, len(P), -nrms))
        return

    best = (0, None)
    nb = min(a.nbright, len(P))
    rmax = float(np.max([np.hypot(*(P[j] - P[i])) for i in range(nb) for j in range(nb)])) + a.tol
    cp = tree.query_pairs(rmax, output_type="ndarray")
    cp = np.vstack([cp, cp[:, ::-1]])                      # both orientations
    cd = np.hypot(*(C[cp[:, 1]] - C[cp[:, 0]]).T)
    print("catalogue pairs within %.0f\": %d (%.1fs)" % (rmax, len(cp), time.time() - t0))
    tested = 0
    goal = max(6, int(0.6 * len(P)))
    seeds = sorted(((i, j) for i in range(nb) for j in range(i + 1, nb)), key=lambda p: p[0] + p[1])
    for i, j in seeds:
        dij = np.hypot(*(P[j] - P[i]))
        if dij < 60:
            continue
        sel = cp[np.abs(cd - dij) < a.tol]
        for parity in (1, -1):
            Q = P.copy(); Q[:, 1] *= parity
            vq = Q[j] - Q[i]
            aq = math.atan2(vq[1], vq[0])
            for A, B in sel:
                vc = C[B] - C[A]
                ang = math.atan2(vc[1], vc[0]) - aq
                ca, sa = math.cos(ang), math.sin(ang)
                R = np.array([[ca, -sa], [sa, ca]])
                T = (Q - Q[i]) @ R.T + C[A]
                dist, idx = tree.query(T, distance_upper_bound=a.tol)
                n = int(np.isfinite(dist).sum())
                tested += 1
                if n > best[0]:
                    best = (n, dict(i=i, j=j, A=A, B=B, parity=parity, ang=ang, T=T, idx=idx, dist=dist))
        print("  seed pair (%d,%d) d=%.0f\": %d catalogue pairs, best so far %d/%d (%.1fs)" % (i, j, dij, len(sel), best[0], len(P), time.time() - t0))
        if best[0] >= goal:
            break
    n, b = best
    print("tested %d hypotheses in %.1fs; best matches %d of %d" % (tested, time.time() - t0, n, len(P)))
    if not b or n < 5:
        sys.exit("NO SOLUTION")
    # field centre = image (W/2,H/2) -> transformed position of origin
    Q0 = np.array([0.0, 0.0])
    Qi = P[b["i"]].copy(); Qi[1] *= b["parity"]
    ang = b["ang"]
    R = np.array([[math.cos(ang), -math.sin(ang)], [math.sin(ang), math.cos(ang)]])
    c = (Q0 - Qi) @ R.T + C[b["A"]]
    ra_c, dec_c = deproject(c[0], c[1], a.ra0, a.dec0)
    ok = np.isfinite(b["dist"])
    resid = b["dist"][ok]
    print("SOLVED: centre RA %.5f deg (%.5f h) Dec %+.5f ; parity %+d ; rotation %.1f deg ; resid rms %.2f\""
          % (ra_c, ra_c / 15, dec_c, b["parity"], math.degrees(ang) % 360, math.sqrt((resid ** 2).mean())))
    print("offset of field centre from search centre: dRA*cosDec %+.1f' dDec %+.1f'" % (c[0] / 60, c[1] / 60))
    # where on the image does sky-east and north point
    Rinv = R.T
    e = Rinv @ np.array([1.0, 0.0]); nn = Rinv @ np.array([0.0, 1.0])
    e[1] *= b["parity"]; nn[1] *= b["parity"]
    print("on the image (x right, y down): east (%+.3f,%+.3f) north (%+.3f,%+.3f)" % (e[0], e[1], nn[0], nn[1]))


if __name__ == "__main__":
    main()
