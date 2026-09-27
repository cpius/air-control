#!/usr/bin/env python3
"""Tighten one azimuth's edge between a known-blocked and a known-open altitude.

A full ladder.run re-derives the ceiling from scratch -- 45/60/75 probes, then
bisects the whole 3-75 range. When the bracket is already known, all of that is
wasted sky time. This bisects only the gap.

Usage: refine.py <az> <blocked_alt> <open_alt> [steps]
"""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from skysurvey import Rig, shot, fmt
import survey_report

def main():
    az   = float(sys.argv[1])
    lo   = float(sys.argv[2])          # known blocked
    hi   = float(sys.argv[3])          # known open
    n    = int(sys.argv[4]) if len(sys.argv) > 4 else 5
    rig  = Rig()
    print(f"\n=== az {az:.0f}: refining edge in {lo:.1f}-{hi:.1f} ({n} steps) ===", flush=True)
    for k in range(n):
        if hi - lo < 0.4:
            print(f"   converged below 0.4 deg after {k} steps", flush=True)
            break
        mid = (lo + hi) / 2.0
        exp = 2.0 if mid >= 40 else 3.0
        r = shot(rig, az, mid, exp=exp, cap=75.0,
                 label=f"az{az:.0f} edge refine {k+1}/{n}")
        print("   " + fmt(r), flush=True)
        survey_report.render()
        if r.get("solved"):
            hi = r["true_alt"]          # anchor to where the solve says we were
        elif r.get("verdict") == "AMBIGUOUS":
            print("   ambiguous — not evidence either way; stopping", flush=True)
            break
        else:
            lo = mid
    print(f"   -> az {az:.0f}: edge between {lo:.1f} and {hi:.1f}  (gap {hi-lo:.2f})", flush=True)

if __name__ == "__main__":
    main()
