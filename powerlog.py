#!/usr/bin/env python3
"""Every --every seconds: mount input voltage (4400), Air temperature / undervolt (4700), cooler state -> one log line."""
import argparse, os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from air_rpc import Air
ap = argparse.ArgumentParser(); ap.add_argument("--every", type=float, default=300); ap.add_argument("--host", default="192.168.1.35"); a = ap.parse_args()
KEY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "embedded_key.pem")
while True:
    out = []
    try:
        m = Air(a.host, 4400, timeout=8); i = m.call("scope_get_info", [], timeout=8)["result"]; m.close()
        out.append(f"mount {i.get('input_voltage')} mV track {i.get('is_enable_track')} RA {i.get('RA'):.4f} Dec {i.get('Dec'):.4f}")
    except Exception as e: out.append(f"mount ? ({e.__class__.__name__})")
    try:
        c = Air(a.host, 4700, timeout=8, key=KEY); p = c.call("pi_get_info", [], timeout=8)["result"]
        out.append(f"Air {p.get('temp')}C undervolt {p.get('is_undervolt')}")
        for name in ("CoolerOn", "CoolPowerPerc", "Temperature"):
            try:
                r = c.call("get_control_value", [name], timeout=8); r = r.get("result", r); out.append(f"{name} {r.get('value') if isinstance(r, dict) else r}")
            except Exception: pass
        c.close()
    except Exception as e: out.append(f"Air ? ({e.__class__.__name__})")
    print(f"{time.strftime('%H:%M:%S')} POWER " + " | ".join(out), flush=True)
    time.sleep(a.every)
