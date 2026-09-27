#!/usr/bin/env python3
"""Find the roofline at one azimuth by solve-verified bisection.

The problem this exists to solve: a BLOCKED point has no plate solve, so its
only position is the mount's register -- and on 2026-08-25 the register lied by
9 deg twice, because a cable bind stalled the Dec axis while the encoder counted
the full travel. An unverified BLOCKED point is worthless.

The fix: never trust a blocked altitude until a solve nearby proves the mount
was really there. After each blocked shot we step UP a little and solve. That
shot is useful survey data anyway, and it pins down where the mount physically
was:

  * solve lands where commanded  -> the mount is tracking commands; the blocked
    point below it was genuinely reached, and the bracket is real.
  * solve lands short            -> the mount is stuck. The solve tells us where
    it actually is, every blocked reading since the last solve is VOID, and the
    run stops rather than writing fiction.
"""
import argparse, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from skysurvey import (Rig, shot, fmt, OUT, radec_to_altaz)
from airlog import get_logger
import survey_report

log = get_logger("ladder")
STALL_TOL = 1.5        # deg: register-vs-truth beyond this means the axis slipped
VERIFY_UP = 6.0        # deg to step up when confirming a blocked reading


def refresh():
    survey_report.render()


def exp_for(alt):
    """Longer low down: extinction eats the faint stars exactly where the
    roofline is, which would make open sky look blocked."""
    if os.environ.get("LADDER_EXP"):
        return float(os.environ["LADDER_EXP"])
    # Dark-sky values, measured 2026-08-26 23:24 with the sky fully dark:
    #   alt 18, 3s -> 128 stars   (5s -> 145: extinction-limited, longer buys nothing)
    #   alt 70, 2s -> 482 stars   (4s -> 649; 10s gave 1716 and the solver ground 75s)
    # Too MANY stars is a real failure mode up high -- the match degenerates in a
    # crowded field -- so this gets shorter with altitude, not longer.
    return 2.0 if alt >= 40 else (3.0 if alt >= 20 else 4.0)


def take(rig, az, alt, label):
    r = shot(rig, az, alt, exp=exp_for(alt), cap=75.0, label=label)
    print("   " + fmt(r), flush=True)
    refresh()
    return r


def stalled(r):
    """A solved shot whose truth disagrees with the register = slipped axis."""
    if not r.get("solved") or "reg_alt" not in r:
        return False
    return (abs(r.get("off_alt", 0)) > STALL_TOL or abs(r.get("off_az", 0)) > STALL_TOL)


def void_since(azimuth, note):
    """Mark unverified blocked points at this azimuth as VOID."""
    recs = [json.loads(l) for l in open(OUT) if l.strip()]
    n = 0
    for r in reversed(recs):
        if r.get("verdict") == "OPEN":
            break
        if r.get("verdict") == "BLOCKED" and r.get("want_az") == azimuth:
            r["verdict"] = "VOID"; r["note"] = note; n += 1
    open(OUT, "w").write("\n".join(json.dumps(r) for r in recs) + "\n")
    refresh()
    return n


def run(az, hi=30.0, lo=3.0, passes=4):
    rig = Rig()
    print(f"\n=== azimuth {az}° — roofline by solve-verified bisection ===", flush=True)

    # 1. establish an OPEN ceiling, climbing if need be
    top = None
    for a in (hi, hi + 15, hi + 30):
        r = take(rig, az, a, f"az{az:.0f} ceiling probe")
        if r.get("solved"):
            top = r["true_alt"]; break
        if r.get("verdict") == "ERROR":
            print("   aborting: " + str(r.get("error"))); return
    if top is None:
        print(f"   no open sky found up to {hi+30:.0f}° — leaving azimuth {az}°")
        return

    # 2. bisect between the open ceiling and a low floor
    bot = lo
    bot_confirmed = False
    for k in range(passes):
        mid = (top + bot) / 2.0
        if top - bot < 1.2:
            break
        r = take(rig, az, mid, f"az{az:.0f} bisect {k+1}")
        if r.get("verdict") == "ERROR":
            print("   aborting: " + str(r.get("error"))); return
        if stalled(r):
            n = void_since(az, f"VOID - axis slipped {r.get('off_alt'):+.1f}deg in alt; register unreliable")
            print(f"   !! AXIS SLIPPED ({r.get('off_alt'):+.2f}° alt) — voided {n} readings, stopping this azimuth")
            return
        if r.get("solved"):
            top = r["true_alt"]
        elif r.get("verdict") == "AMBIGUOUS":
            print("   ambiguous (solver ground, stars present) — not a roofline; stopping")
            return
        else:
            # blocked: prove the mount was really there before believing it
            v = take(rig, az, mid + VERIFY_UP, f"az{az:.0f} verify blocked @{mid:.1f}")
            if v.get("solved") and not stalled(v):
                bot = mid; bot_confirmed = True          # blocked reading stands
                top = min(top, v["true_alt"])
            elif v.get("solved") and stalled(v):
                n = void_since(az, f"VOID - verify solve landed {v.get('off_alt'):+.1f}deg off; mount stuck")
                print(f"   !! MOUNT STUCK — voided {n} readings, stopping this azimuth")
                return
            else:
                bot = mid                                # both blocked: roof is higher
    print(f"   -> az {az}°: roof between {bot:.1f}° and {top:.1f}°"
          f"{'' if bot_confirmed else ' (lower bound unconfirmed)'}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("az", type=float)
    ap.add_argument("--hi", type=float, default=30.0)
    ap.add_argument("--lo", type=float, default=3.0)
    ap.add_argument("--passes", type=int, default=4)
    a = ap.parse_args()
    run(a.az, a.hi, a.lo, a.passes)
