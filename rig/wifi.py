#!/usr/bin/env python3
"""Inspect and steer the Air's Wi-Fi station link (4700).

Why this exists: the Air joins 5 GHz when both bands are visible, and 5 GHz
does not survive the trip to the east balcony. At -70 dBm the 4400/4700 sockets
start timing out, and a dropped `scope_move ["none"]` leaves the mount slewing
until it hits its own ~12.4 deg cap. Band choice is a safety issue, not comfort.

The app gives no band control, but the firmware does:

    pi_station_state        CMD_STATION_GET_STATE          what we are on now
    pi_station_scan         CMD_STATION_SCAN               what is visible, with band
    pi_station_list_config  CMD_STATION_GET_SETUP_LIST     saved networks
    pi_station_select       CMD_STATION_CONNECT_ONE_NETWORK  join a saved network
    pi_station_set          CMD_STATION_SET_ONE_NETWORK    add/---configure one
    pi_station_remove / pi_station_open / pi_station_close

Note `pi_set_5g` (CMD_SET_AP_5G) is the band of the Air's OWN hotspot, not the
station link -- a different knob that is easy to mistake for this one.

    python3 wifi.py state
    python3 wifi.py scan            # SSIDs with frequency, so you can see the bands
    python3 wifi.py list            # saved networks
    python3 wifi.py select <arg>    # join one (MOVES THE LINK -- may drop you)
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from air_rpc import Air
from airlog import add_log_args, configure_logging, get_logger

log = get_logger("wifi")
KEY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "embedded_key.pem")


def band(freq):
    """MHz -> human band. 2412-2484 is 2.4 GHz; 5150-5895 is 5 GHz."""
    try:
        f = int(freq)
    except (TypeError, ValueError):
        return "?"
    if 2400 <= f <= 2500:
        return "2.4GHz"
    if 5000 <= f <= 5900:
        return "5GHz"
    return f"{f}MHz"


def quality(sig):
    """Signal level in dBm -> a word, because -70 is where this rig breaks."""
    try:
        s = int(sig)
    except (TypeError, ValueError):
        return ""
    if s >= -55:
        return "strong"
    if s >= -65:
        return "ok"
    if s >= -72:
        return "MARGINAL - drops commands"
    return "UNUSABLE"


def show(rows):
    """Print whatever shape the firmware returns, pulling out band and signal."""
    if not isinstance(rows, list):
        print(json.dumps(rows, indent=2))
        return
    for r in rows:
        if not isinstance(r, dict):
            print(f"  {r}")
            continue
        ssid = r.get("ssid") or r.get("name") or "?"
        freq = r.get("freq") or r.get("frequency") or r.get("channel")
        sig = r.get("sig_lev") or r.get("signal") or r.get("rssi")
        bits = [f"{ssid!r:28s}"]
        if freq is not None:
            bits.append(f"{band(freq):7s}")
        if sig is not None:
            bits.append(f"{sig:>5} dBm {quality(sig):24s}")
        extra = {k: v for k, v in r.items()
                 if k not in ("ssid", "name", "freq", "frequency", "channel",
                              "sig_lev", "signal", "rssi")}
        if extra:
            bits.append(json.dumps(extra))
        print("  " + " ".join(bits))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ)
    ap.add_argument("--key", default=KEY)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("state"); sub.add_parser("scan"); sub.add_parser("list")
    s = sub.add_parser("select", help="join a saved network -- MOVES THE LINK")
    s.add_argument("arg", help="index or ssid, as `list` reports it")
    add_log_args(ap)
    a = ap.parse_args()
    configure_logging(a)

    air = Air(a.host, 4700, timeout=10, key=a.key)
    try:
        if a.cmd == "state":
            r = air.call("pi_station_state", [], timeout=12)
            print(json.dumps(r.get("result", r), indent=2))
        elif a.cmd == "scan":
            r = air.call("pi_station_scan", [], timeout=30)
            res = r.get("result", r)
            print("visible networks:")
            show(res)
            print("\nA 2.4GHz row for the SSID you use is the one you want. If the "
                  "same SSID appears on both bands the router is band-steering; "
                  "pinning it may need a 2.4-only SSID router-side.")
        elif a.cmd == "list":
            r = air.call("pi_station_list_config", [], timeout=12)
            print("saved networks:")
            show(r.get("result", r))
        elif a.cmd == "select":
            try:
                arg = int(a.arg)
            except ValueError:
                arg = a.arg
            log.warn("joining %r -- the link may drop; rediscover with discover.py", arg)
            r = air.call("pi_station_select", [arg], timeout=25)
            print(json.dumps(r, indent=2))
            if r.get("code") == 107:
                print("\n107 = wrong param shape. The 4700 setters that want an "
                      "object take a LIST-WRAPPED one (as pi_set_time does); "
                      "retry with [{...}] once `list` shows the field names.")
    finally:
        air.close()


if __name__ == "__main__":
    main()
