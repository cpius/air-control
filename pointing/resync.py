#!/usr/bin/env python3
"""Rebuild the mount's pointing register from a plate solve, walking the sync in.

Why walking: the AM5N silently refuses (or clamps) a sync more than ~9 deg from
where its register currently sits, so a large correction must be applied in
steps. Verified 2026-08-24: an 11.8 deg error went in cleanly as three 4 deg steps.

A sync moves the REGISTER, not the telescope -- nothing physically moves, which
is why reported Alt/Az shifts while the OTA sits still.

This matters for the horizon survey specifically: a BLOCKED point has no solve,
so its only position is the register. The register must be true or the map lies.
"""
import json, math, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from air_rpc import Air
from airlog import get_logger
from skysurvey import Rig, expose, plate_solve, radec_to_altaz

log = get_logger("resync")
MAX_STEP = 4.0            # degrees per sync; well inside the ~9 deg refusal cap


def angsep(ra1, d1, ra2, d2):
    """Great-circle separation in degrees. RA in hours."""
    r1, r2 = math.radians(ra1 * 15), math.radians(ra2 * 15)
    p1, p2 = math.radians(d1), math.radians(d2)
    c = (math.sin(p1) * math.sin(p2) +
         math.cos(p1) * math.cos(p2) * math.cos(r1 - r2))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def main():
    rig = Rig()
    rig.ensure_tracking()

    print("1. solving to find where we truly are")
    ok, _ = expose(rig, 10.0)
    if not ok:
        print("   exposure failed"); return 1
    r, dt, st = plate_solve(rig, cap=75.0)
    if r is None:
        print(f"   NO SOLVE ({st}, {dt:.1f}s) — cannot resync blind"); return 1
    tra, tdec = r["ra_dec"]
    print(f"   truth: RA {tra:.5f}h Dec {tdec:+.5f}  ({r['star_number']} stars, {dt:.1f}s)")

    for step in range(1, 8):
        i = rig.state()
        cra, cdec = i["RA"], i["Dec"]
        sep = angsep(cra, cdec, tra, tdec)
        print(f"\n{step+1}. register RA {cra:.5f}h Dec {cdec:+.5f}  -> off by {sep:.3f} deg")
        if sep < 0.15:
            print("   register is true — done")
            break
        # Step at most MAX_STEP degrees toward the truth, in both axes.
        f = min(1.0, MAX_STEP / sep)
        dra = ((tra - cra + 12) % 24) - 12          # shortest way round in RA
        nra, ndec = (cra + dra * f) % 24, cdec + (tdec - cdec) * f
        print(f"   sync -> RA {nra:.5f}h Dec {ndec:+.5f}  (moving {sep*f:.2f} deg)")
        resp = rig.mnt("scope_sync", [nra, ndec], t=15)
        if resp.get("code", 0) not in (0, None):
            print(f"   REFUSED: {resp.get('error')} (code {resp.get('code')})")
            return 1
        time.sleep(1.5)

    print("\n   confirming with a fresh solve")
    ok, _ = expose(rig, 10.0)
    r2, dt2, st2 = plate_solve(rig, cap=75.0)
    if r2 is None:
        print(f"   confirm solve failed ({st2})"); return 1
    i = rig.state()
    sep = angsep(i["RA"], i["Dec"], r2["ra_dec"][0], r2["ra_dec"][1])
    alt, az = radec_to_altaz(r2["ra_dec"][0], r2["ra_dec"][1], i["sidereal_time"])
    hz = rig.horiz()
    print(f"   solved  RA {r2['ra_dec'][0]:.5f}h Dec {r2['ra_dec'][1]:+.5f}  ({r2['star_number']} stars)")
    print(f"   register RA {i['RA']:.5f}h Dec {i['Dec']:+.5f}")
    print(f"   RESIDUAL: {sep:.3f} deg   ({'GOOD' if sep < 0.5 else 'STILL OFF'})")
    print(f"   horizon: register alt {hz[0]:.2f} az {hz[1]:.2f} | true alt {alt:.2f} az {az:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
