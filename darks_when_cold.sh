#!/bin/zsh
export ASIAIR_HOST=192.168.1.35
cd /Users/madsdorup/ASICAP/air-control
D="$HOME/ASICAP/2026-08-27 NGC6946 Ha"
echo "waiting for the sensor to reach -9.5C ..."
python3 - <<'PY'
import sys, time
sys.path.insert(0,'/Users/madsdorup/ASICAP/air-control')
from air_rpc import Air
KEY='/Users/madsdorup/ASICAP/air-control/embedded_key.pem'
deadline=time.time()+1800
while time.time()<deadline:
    try:
        a=Air("192.168.1.35",4700,key=KEY,timeout=15); a.verify(KEY)
        a.drain_events(); t0=time.time(); temp=None; power=None
        while time.time()-t0<20:
            for e in a.drain_events():
                if e.get("Event")=="Temperature": temp=e.get("value")
                if e.get("Event")=="CoolerPower": power=e.get("value")
            time.sleep(1)
        a.close()
        print("   sensor %.1fC  cooler %s%%" % (temp if temp is not None else 99, power), flush=True)
        if temp is not None and temp <= -9.5:
            print("   at setpoint", flush=True); break
    except Exception as e:
        print("   poll failed: %s" % str(e)[:60], flush=True); time.sleep(5)
PY
echo "starting darks"
python3 -u capture_cal.py dark --exp 120 --gain 252 --count 20 --out "$D/Dark"
