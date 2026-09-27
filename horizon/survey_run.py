#!/usr/bin/env python3
"""Drive the horizon survey over a grid of true (az, alt)."""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from survey import Survey

OUT = "/Users/madsdorup/ASICAP/survey-raw.jsonl"
AZ  = [180, 150, 120, 90, 60, 30, 0, 330, 300, 270, 240, 210]
ALT = [70, 55, 40, 28, 20, 14, 9, 5]

s = Survey(8.46, 7.03, OUT)
s.set_bin(2)
try:
    for az in AZ:
        for alt in ALT:
            try:
                r = s.point(float(az), float(alt))
                if r:
                    print(f"RESULT az {az:3d} alt {alt:2d} -> src={r['sources']:3d} "
                          f"med={r['median']:6.0f} true={r['az']:6.2f}/{r['alt']:5.2f} "
                          f"exp={r['exp']}s conv={r['converged']}", flush=True)
                else:
                    print(f"RESULT az {az:3d} alt {alt:2d} -> below floor", flush=True)
            except Exception as e:
                print(f"RESULT az {az:3d} alt {alt:2d} -> ERROR {type(e).__name__}: {str(e)[:60]}", flush=True)
                time.sleep(2)
finally:
    try: s.close()
    except Exception: pass
    print("SURVEY DONE", flush=True)
