#!/usr/bin/env python3
"""Put a target on the sensor centre: solve, sync, goto, repeat.

The sync is the point. `scope_goto` is accurate against the mount's OWN
register, and that register is only as true as the last sync. On a rig whose
polar alignment is off, the sky slides out from under it — so a register synced
ten minutes ago aims wide, and the goto lands nowhere near the target while
reporting success.

Two things the Air does not tell you:

  * **`start_solve` does not sync.** Verified with the register more than 11 deg
    from the sky: a clean 800-star solve changed it by nothing at all.
  * **The Air's auto-sync-on-solve is capped, and fails silently past the cap** —
    the same ~9 deg limit `scope_sync` has. Inside the cap it works and every
    later solve keeps the register honest; outside it, nothing happens and it
    reads exactly like "solving is broken".

So a badly wrong register has to be walked in once, by hand. `sync_to` does
that: a sync only rewrites the register, nothing moves, so it can be applied in
as many small steps as the gap needs. Three ~4 deg steps have taken an 11.8 deg
error onto the solved position to under an arcsecond.

The solver itself needs no hint — it solved fine with the register 11.8 deg
wrong — so a bad register never blocks recovery.
"""
import argparse
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from session import Session
from solving import solve

SYNC_STEP_DEG = 4.0          # comfortably inside the ~9 deg scope_sync cap


def sep_arcmin(ra1, d1, ra2, d2):
    a1, a2 = math.radians(ra1 * 15), math.radians(ra2 * 15)
    b1, b2 = math.radians(d1), math.radians(d2)
    c = math.sin(b1) * math.sin(b2) + math.cos(b1) * math.cos(b2) * math.cos(a1 - a2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c)))) * 60.0


def sync_to(mt, ra, dec, step_deg=SYNC_STEP_DEG):
    """Sync the register onto (ra, dec), walking it in if the jump is too big.

    Returns the number of syncs used.
    """
    st = mt.state()
    ra0, dec0 = st["RA"], st["Dec"]
    gap = sep_arcmin(ra0, dec0, ra, dec) / 60.0
    n = max(1, int(math.ceil(gap / step_deg)))
    for k in range(1, n + 1):
        mt.sync(ra0 + (ra - ra0) * k / n, dec0 + (dec - dec0) * k / n)
        time.sleep(0.4)
    return n


def center(s, ra, dec, tol=3.0, tries=4, exp=4.0, gain=300, page="preview",
           binning=2, say=print):
    """Solve/sync/goto until the field centre is within `tol` arcmin.

    Returns the final solve result, or None if it never converged.
    """
    s.page(page, exp, gain, binning=binning)
    for i in range(tries):
        s.fresh(2)
        r = solve(s.air, timeout=30.0)
        if not r:
            say("  attempt %d: no solve" % (i + 1))
            continue
        sra, sdec = r["ra_dec"]
        off = sep_arcmin(sra, sdec, ra, dec)
        say("  attempt %d: at RA %.5f Dec %.5f — %.1f' from target (%d stars)"
            % (i + 1, sra, sdec, off, r["star_number"]))
        if off <= tol:
            return r
        mt = s.mount()
        n = sync_to(mt, sra, sdec)
        say("    synced register in %d step(s), slewing" % n)
        s.mount().goto(ra, dec, timeout=120)
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ra", type=float, help="target RA in hours (JNow)")
    ap.add_argument("dec", type=float, help="target Dec in degrees (JNow)")
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--key", default="embedded_key.pem")
    ap.add_argument("--tol", type=float, default=3.0, help="arcmin")
    ap.add_argument("--tries", type=int, default=4)
    ap.add_argument("--exp", type=float, default=4.0)
    ap.add_argument("--gain", type=int, default=300)
    a = ap.parse_args()
    if not a.host:
        sys.exit("need --host or ASIAIR_HOST (the Air's IP moves — run discover.py)")
    s = Session(a.host, a.key)
    try:
        r = center(s, a.ra, a.dec, tol=a.tol, tries=a.tries, exp=a.exp, gain=a.gain)
        print("centred: %s" % (r["ra_dec"] if r else "NO — did not converge"))
    finally:
        s.close()


if __name__ == "__main__":
    main()
