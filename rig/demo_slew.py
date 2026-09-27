#!/usr/bin/env python3
"""Small, self-returning demonstration slew of the AM5N (port 4400).

Reads the start position, slews 5 deg in Dec (same RA), confirms motion, then
returns to the exact start and restores the original tracking state. Aborts the
slew on any error. Refuses if the computed move would exceed 10 deg.

    ASIAIR_HOST=<air-ip> python3 demo_slew.py      # or: python3 demo_slew.py <air-ip>
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from airlog import get_logger
from mount import Mount

log = get_logger("demo")

STEP_DEG = 5.0
SETTLE_TIMEOUT = 60
POLL_S = 1.0            # one status line a second while the mount is moving

HOST = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ASIAIR_HOST")
if not HOST:
    sys.exit("usage: python3 demo_slew.py <host>   (or set ASIAIR_HOST)")
m = Mount(HOST)


def snap():
    s = m.state()
    return s["RA"], s["Dec"], s["Alt"], s["Az"], s.get("move_status"), s.get("is_enable_track")


def wait_settled(target_dec, label):
    """Poll once a second until the mount is idle on target. A 5 deg slew is
    tens of seconds of real motion, so silence here would be the bulk of the
    run."""
    t0 = time.time()
    while time.time() - t0 < SETTLE_TIMEOUT:
        ra, dec, alt, az, mv, trk = snap()
        log.info("[%s] %5.1fs  RA=%.4fh Dec=%.3f Alt=%.2f move=%s (%.3f to go)",
                 label, time.time() - t0, ra, dec, alt, mv, dec - target_dec)
        print(f"  [{label}] RA={ra:.4f}h Dec={dec:.3f}° Alt={alt:.2f}° move={mv}", flush=True)
        if (mv in (None, "none")) and abs(dec - target_dec) < 0.25:
            log.info("[%s] settled in %.1fs", label, time.time() - t0)
            return True
        time.sleep(POLL_S)
    log.error("[%s] not settled within %ds", label, SETTLE_TIMEOUT)
    return False


try:
    ra0, dec0, alt0, az0, mv0, trk0 = snap()
    print(f"START  RA={ra0:.4f}h Dec={dec0:.3f}° Alt={alt0:.2f}° Az={az0:.1f}° "
          f"tracking={trk0} move={mv0}\n")

    target = dec0 - STEP_DEG
    if not (-89 < target < 89):          # keep away from the Dec=±90 singularity
        target = dec0 + STEP_DEG
    if abs(target - dec0) > 10:
        raise SystemExit("refusing: computed slew > 10°")

    print(f"SLEW → RA={ra0:.4f}h Dec={target:.3f}°  (Δ{target-dec0:+.1f}° in Dec)")
    print("  scope_goto ->", m.goto(ra0, target))
    ok = wait_settled(target, "out")
    print(f"  reached target: {ok}\n")

    print(f"RETURN → RA={ra0:.4f}h Dec={dec0:.3f}° (home)")
    print("  scope_goto ->", m.goto(ra0, dec0))
    wait_settled(dec0, "back")

    # restore original tracking state (goto may have enabled it)
    ra1, dec1, alt1, az1, mv1, trk1 = snap()
    if trk1 != trk0:
        print(f"\nrestoring tracking → {trk0}:", m.set_tracking(bool(trk0)))
    print(f"\nFINAL  RA={ra1:.4f}h Dec={dec1:.3f}° Alt={alt1:.2f}°  "
          f"(Δ from start: {dec1-dec0:+.3f}° Dec)")
except Exception as e:
    log.error("aborting the slew: %s", e)
    print("\n!! error — aborting slew:", e)
    try:
        print("abort ->", m.abort())
    except Exception as e2:
        print("abort failed:", e2)
finally:
    m.close()
