#!/usr/bin/env python3
"""Drive the roofline ladder across the west balcony's open arc.

The 2026-08-15 west scan measured which SECTORS have sky, all at 45 deg
altitude. What it never measured is how LOW each sector stays open -- which is
the number you actually need to plan a target. This walks the arc and finds the
roofline azimuth by azimuth.

Order is chosen so the most useful sky is measured first: the run may be cut
short by cloud, battery or the mount, and if it is, the west and north-west --
where the Plough and later Deneb/Vega sit -- are already done.
"""
import os, sys, time, traceback
sys.path.insert(0, '/Users/madsdorup/ASICAP/air-control')
import ladder

#  west/NW first, then swing south-west, then north, then the arc edges
AZ = [300, 280, 320, 260, 340, 240, 0, 220, 20, 200, 350, 310, 290, 270,
      330, 250, 230, 10, 210, 190, 30]

def main():
    #  resumable: pass the azimuths still owed as arguments after a restart
    az_list = [float(a) for a in sys.argv[1:]] or AZ
    t0 = time.time()
    for i, az in enumerate(az_list, 1):
        print(f"\n########## [{i}/{len(az_list)}] azimuth {az}   "
              f"({(time.time()-t0)/60:.0f} min elapsed)", flush=True)
        try:
            ladder.run(float(az), hi=float(os.environ.get("LADDER_HI", 30.0)), lo=3.0,
                       passes=int(os.environ.get("LADDER_PASSES", 4)))
        except KeyboardInterrupt:
            print("interrupted", flush=True); return
        except Exception:
            print("AZIMUTH FAILED:\n" + traceback.format_exc(), flush=True)
            time.sleep(3)
    print(f"\nLADDER RUN DONE in {(time.time()-t0)/60:.0f} min", flush=True)

if __name__ == "__main__":
    main()
