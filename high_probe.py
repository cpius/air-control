#!/usr/bin/env python3
"""Probe the TOP of the sky -- the half ladder.py structurally cannot see.

ladder.py bisects downward from an open ceiling, so it never samples above 60
deg. On a balcony that is a real blind spot: the floor of the flat above can cut
off the zenith, which would make high-altitude sky *worse* than low. A blank
region on the map reads as open to whoever uses it next, so this measures it
rather than leaving it inferred.

Sampled as a plain grid, not a bisection -- there is no reason to assume a
single monotonic boundary up here, and a grid cannot smuggle that assumption in.
Two azimuths in the known-blocked sector (190, 150) are included on purpose: if
the side wall stops below 70 deg, sky opens up over the top of it, and that is
worth knowing.
"""
import os, sys, time
sys.path.insert(0, '/Users/madsdorup/ASICAP/air-control')
from skysurvey import Rig, shot, fmt
import survey_report

# Where 60 deg is already CLOSED -- the walls whose top edge is unknown. 195
# turned out to stop at ~51 deg, so "blocked to 60" may well have sky above it.
WALLS = [190, 180, 170, 150]
# Two zenith checks only. A balcony ceiling is the one obstruction that blocks
# HIGH while leaving low open, so an open 30 deg does not rule it out -- but two
# samples across the arc are enough to find a soffit if there is one.
CEILING = [270, 0]
ALT = [70, 80, 85]

def main():
    rig, t0 = Rig(), time.time()
    print(f"\n=== high probe: {len(WALLS)} walls x {len(ALT)} alts + {len(CEILING)} zenith checks ===", flush=True)
    plan = [(az, alt) for az in WALLS for alt in ALT] + [(az, 85) for az in CEILING]
    for az, alt in plan:
        if True:
            try:
                r = shot(rig, float(az), float(alt), exp=10.0, cap=75.0,
                         label=f"high probe az{az} alt{alt}")
                print("   " + fmt(r), flush=True)
                survey_report.render()
            except Exception as e:
                print(f"   az {az} alt {alt} ERROR {type(e).__name__}: {str(e)[:70]}", flush=True)
                time.sleep(2)
    print(f"\nHIGH PROBE DONE in {(time.time()-t0)/60:.0f} min", flush=True)

if __name__ == "__main__":
    main()
