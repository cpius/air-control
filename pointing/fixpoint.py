#!/usr/bin/env python3
"""Solve (ASTAP, our own detections) -> sync the register to the JNow truth (walked in) -> goto the
target -> repeat until the target sits within --tol arcmin of the frame centre.

The register is read BEFORE each frame, so the error printed is the real one. Pipes.grab stops the
Air's own preview solve, so nothing auto-syncs behind our back. A fresh 4400 socket per mount
phase: 4400 drops idle sockets during downloads/solves. Every sync reply code is checked, and a
refusal stops the run -- the AM5N caps cumulative sync at ~9.5 deg after Go Home (then 207).

Needs a FOCUSED frame (ASTAP via pointing/astapsolve.py). Targets and hints are J2000; the mount
gets JNow. --hint-ra/--hint-dec: where the scope really points (a previous solve), not the register.

2026-10-19, reducer, east balcony: after a power-cycle Go Home the register was 4.8 deg off; walked
in as 2 syncs -> first goto 7.2' off -> re-sync -> Capella 0.3' from centre. A 30 deg goto from
that anchor then missed Aldebaran by 41' (polar-alignment error, bigger than the 28'x16' field);
two more iterations -> 0.1'. Run it at every new target.

    ASIAIR_HOST=... python3 -u pointing/fixpoint.py --target-ra 5.277936 --target-dec 45.997991 \
        --hint-ra 5.033 --hint-dec 42.08
"""
import argparse, math, os, re, subprocess, sys, time
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
from daypipes import Pipes, log, host
from mount import Mount
from findstar import jnow

ap = argparse.ArgumentParser()
ap.add_argument("--target-ra", type=float, required=True, help="J2000 hours")
ap.add_argument("--target-dec", type=float, required=True, help="J2000 deg")
ap.add_argument("--hint-ra", type=float, required=True, help="J2000 hours of where the scope really points now")
ap.add_argument("--hint-dec", type=float, required=True)
ap.add_argument("--epoch", type=float, default=2000.0 + (time.time() - 946728000.0) / (365.25 * 86400),
                help="JNow epoch, decimal year (default: now)")
ap.add_argument("--fov", type=float, default=0.2661, help="frame height deg (1080 x 0.887\"/px)")
ap.add_argument("--tol", type=float, default=2.0, help="arcmin")
ap.add_argument("--iters", type=int, default=4)
ap.add_argument("--max-step", type=float, default=2.5, help="deg per sync step")
ap.add_argument("--exp", type=float, default=2.0); ap.add_argument("--gain", type=int, default=100)
ap.add_argument("--outdir", default=os.path.expanduser("~/ASICAP/telemetry/%s/frames" % time.strftime("%Y-%m-%d")))
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)


def sep(ra1, d1, ra2, d2):
    r1, r2, p1, p2 = map(math.radians, (ra1 * 15, ra2 * 15, d1, d2))
    c = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(r1 - r2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c)))) * 60.0


def astap(npy, ra, dec, radius):
    out = subprocess.run([sys.executable, "-u", os.path.join(HERE, "astapsolve.py"), "--npy", npy,
                          "--ra", "%.5f" % ra, "--dec", "%.4f" % dec, "--radius", str(radius),
                          "--fov", "%.4f" % a.fov, "--max", "60"], capture_output=True, text=True, timeout=300).stdout
    m = re.search(r"SOLVED centre ICRS RA ([\d.]+) deg \(([\d.]+)h\) Dec ([+-][\d.]+) ; scale ([\d.]+)", out)
    q = re.search(r"(\d+) of (\d+) quads", out)
    if not m:
        log("  ASTAP: no solve\n" + "\n".join("     " + l for l in out.splitlines()[-6:]))
        return None
    return float(m.group(2)), float(m.group(3)), float(m.group(4)), (q.group(0) if q else "?")


def register():
    m = Mount(host())
    try:
        s = m.state()
        return s["RA"], s["Dec"], s
    finally:
        m.close()


tra, tdec = jnow(a.target_ra, a.target_dec, a.epoch)
log("target J2000 %.5fh %+.4f -> JNow %.5fh %+.4f" % (a.target_ra, a.target_dec, tra, tdec))
hint = (a.hint_ra, a.hint_dec, 5.0)
p = Pipes()
synced_total = 0.0
try:
    p.setup("preview", a.exp, a.gain, 2)
    for it in range(a.iters):
        rra, rdec, st = register()
        log("iter %d: register RA %.5f Dec %+.4f (alt %.1f az %.1f, pier %s, track %s)" % (it + 1, rra, rdec, st["Alt"], st["Az"], st["pier_side"], st["is_enable_track"]))
        img, w, h, info = p.grab()
        fn = os.path.join(a.outdir, "%s_fixpoint_i%d.npy" % (time.strftime("%H%M%S"), it + 1))
        np.save(fn, img.astype(np.uint16))
        r = astap(fn, *hint)
        if not r:
            log("no solve -- stopping (nothing synced on a guess)"); break
        sra2k, sdec2k, scale, quads = r
        sra, sdec = jnow(sra2k, sdec2k, a.epoch)
        err = sep(rra, rdec, sra, sdec); off = sep(sra, sdec, tra, tdec)
        dra = ((rra - sra + 12) % 24 - 12) * 15 * 60 * math.cos(math.radians(sdec)); ddec = (rdec - sdec) * 60
        log("  solved J2000 %.5fh %+.4f (%.4f\"/px, %s) = JNow %.5fh %+.4f" % (sra2k, sdec2k, scale, quads, sra, sdec))
        log("  register error %.1f' (RA %+.1f' Dec %+.1f')   target is %.1f' from frame centre" % (err, dra, ddec, off))
        hint = (sra2k, sdec2k, 3.0)
        if off < a.tol:
            log("ON TARGET within %.1f' after %d iteration(s)" % (a.tol, it + 1)); break
        if err > 0.5:
            n = max(1, int(math.ceil(err / 60.0 / a.max_step)))
            m = Mount(host())
            try:
                for k in range(1, n + 1):
                    ra_k = rra + (((sra - rra + 12) % 24) - 12) * k / n
                    de_k = rdec + (sdec - rdec) * k / n
                    rep = m.air.call("scope_sync", [float(ra_k % 24), float(de_k)], timeout=15)
                    log("  sync %d/%d -> RA %.5f Dec %+.4f : code %s %s" % (k, n, ra_k % 24, de_k, rep.get("code"), rep.get("error", "")))
                    if rep.get("error"):
                        raise SystemExit("sync refused -- stopping (cumulative so far %.2f deg)" % synced_total)
                    time.sleep(0.5)
                synced_total += err / 60.0
                s2 = m.state(); log("  register now RA %.5f Dec %+.4f (%.1f' from the solve); cumulative sync %.2f deg" % (
                    s2["RA"], s2["Dec"], sep(s2["RA"], s2["Dec"], sra, sdec), synced_total))
            finally:
                m.close()
        m = Mount(host())
        try:
            log("  goto target JNow %.5fh %+.4f" % (tra, tdec))
            m.goto(tra, tdec, wait=True, timeout=120)
        finally:
            m.close()
        hint = (a.target_ra, a.target_dec, 3.0)
        time.sleep(1.0)
    else:
        log("not converged in %d iterations" % a.iters)
finally:
    p.close()
