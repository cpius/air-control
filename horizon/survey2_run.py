import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from survey2 import Survey2
OUT="/Users/madsdorup/ASICAP/survey2-raw.jsonl"
AZ=[0,20,40,60,80,100,120,140,160,180,200,220,240,260,280,300,320,340]
ALT=[50,35,24,16,10,6]
s=Survey2(OUT)
try:
    for az in AZ:
        for alt in ALT:
            try:
                r=s.point(float(az), float(alt))
                print(f"RESULT az {az:3d} alt {alt:2d} -> "
                      + (f"src={r['sources']:3d} med={r['median']:6.0f} at {r['az']:6.2f}/{r['alt']:5.2f} exp={r['exp']}s"
                         if r else "skipped"), flush=True)
            except Exception as e:
                print(f"RESULT az {az:3d} alt {alt:2d} -> ERROR {type(e).__name__}: {str(e)[:60]}", flush=True)
                time.sleep(2)
finally:
    try: s.close()
    except Exception: pass
    print("SURVEY2 DONE", flush=True)
