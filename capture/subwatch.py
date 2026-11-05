#!/usr/bin/env python3
"""Follow an autorun target folder on the Air's share, copy each finished sub, measure it with
tools/framecheck.py and print one line per sub (FWHM, ecc, stars) -- flags soft or trailed subs.

    python3 -u capture/subwatch.py --share <mountpoint> --target M1 --night 20261104 \
        --dest "~/ASICAP/2026-11-04 Session/Light/M1" --scale 0.414
"""
import argparse, glob, os, re, shutil, subprocess, time
ap = argparse.ArgumentParser()
ap.add_argument("--share", required=True); ap.add_argument("--target", required=True)
ap.add_argument("--night", required=True, help="YYYYMMDD in the FITS names (UTC date of the start)")
ap.add_argument("--dest", required=True); ap.add_argument("--scale", type=float, default=0.414)
ap.add_argument("--soft", type=float, default=4.5, help="arcsec FWHM above which a sub is flagged")
ap.add_argument("--ecc", type=float, default=1.6, help="eccentricity above which a sub is flagged")
a = ap.parse_args()
dest = os.path.expanduser(a.dest); os.makedirs(dest, exist_ok=True)
src = os.path.join(a.share, "Autorun", "Light", a.target)
seen = set(os.path.basename(p) for p in glob.glob(os.path.join(dest, "*.fit")))
sizes = {}
print("watching %s for *%s-*.fit (already have %d)" % (src, a.night, len(seen)), flush=True)
while True:
    for p in sorted(glob.glob(os.path.join(src, "*_%s-*.fit" % a.night))):
        b = os.path.basename(p)
        if b in seen: continue
        try: s = os.path.getsize(p)
        except OSError: continue
        if sizes.get(b) != s or s < 16_000_000:           # still being written (2880 B short = partial)
            sizes[b] = s; continue
        try:
            shutil.copyfile(p, os.path.join(dest, b)); seen.add(b)   # copyfile, not copy2: the Air's root-only 'arch' flag can't be copied
        except OSError as e:
            print("%s  copy failed for %s: %s" % (time.strftime("%H:%M"), b, e), flush=True); continue
        out = subprocess.run(["python3", os.path.expanduser("~/ASICAP/tools/framecheck.py"), "--scale", str(a.scale), os.path.join(dest, b)],
                             capture_output=True, text=True).stdout
        m = re.search(r"^\S*_(\d{4})\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+(\d+)", out, re.M)
        if m:
            n, stars, fwhm, maj, mn, ecc, pa = m.groups(); fwhm, ecc = float(fwhm), float(ecc)
            flag = "  <-- SOFT" if fwhm > a.soft else ("  <-- TRAILED" if ecc > a.ecc else "")
            print("%s  sub %s  FWHM %.2f\"  ecc %.2f  (%s stars, PA %s)%s" % (time.strftime("%H:%M"), n, fwhm, ecc, stars, pa, flag), flush=True)
        else:
            print("%s  sub %s: no measurement -- %s" % (time.strftime("%H:%M"), b, out.strip().splitlines()[-1:] if out.strip() else "framecheck failed"), flush=True)
    time.sleep(10)
