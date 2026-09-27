#!/usr/bin/env python3
"""Enumerate real ASIAIR RPC method names by probing an authenticated channel.

Oracle: a wrong name returns error code 103 "method not found"; a real name
returns a result or a *different* error (e.g. "device not connected",
"expected object param"), either of which confirms it exists. So: fire a big
read-only wordlist, keep everything that isn't a 103.

Read-only verbs only (get_/is_/has_/scan_ and <device>_get_). Nothing here
moves the mount, exposes, or writes settings.

    python3 find_methods.py --host <air-ip> --key embedded_key.pem
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from air_rpc import Air
from airlog import add_log_args, configure_logging, get_logger

log = get_logger("probe")

# Device nouns the firmware might expose, and read-only attributes to pair.
DEVICES = [
    "mount", "scope", "telescope", "guider", "guide", "guiding",
    "focuser", "eaf", "wheel", "efw", "filter", "rotator",
    "camera", "cam", "dslr", "pi", "air",
]
ATTRS = [
    "state", "info", "status", "position", "coord", "coords",
    "equ_coord", "horiz_coord", "ra_dec", "radec", "altaz",
    "track_state", "tracking", "connected", "connect_state",
    "setting", "settings", "name", "model", "temp", "temperature",
    "speed", "goto_state", "park_state", "align_state",
]

# Curated exact names to also try — spellings ASIAIR is known/suspected to use.
# Read-only + param-requiring names (goto/sync/solve family) probed with EMPTY
# params for existence only: a real one errors on the missing param (revealing
# itself), a wrong one is code 103. No bare motion verbs (park/move/track).
CURATED = [
    # discovery / devices
    "get_connected_devices", "get_connect_devices", "scan_air", "scan_devices",
    "scan_mount", "scan_focuser", "scan_wheel", "scan_guider",
    "get_all_devices", "get_devices", "get_supported_devices", "get_dev_state",
    # goto / target / pointing (the "goto" vocabulary from settings)
    "goto_target", "start_goto_target", "stop_goto_target", "stop_goto",
    "get_goto_target", "get_goto_state", "get_goto_info", "is_goto",
    "goto_ra_dec", "goto_radec", "goto_horiz", "goto_by_horiz",
    "get_target", "get_current_target", "get_last_target",
    "sync_target", "start_sync", "get_sync_state",
    "get_equ_coord", "get_horiz_coord", "get_ra_dec", "get_coord",
    "get_track_state", "get_tracking", "get_slew_state", "get_pier_side",
    "get_park_state", "is_parked", "is_tracking", "is_slewing",
    "get_meridian", "get_flip_setting", "get_merid_flip",
    "get_autogoto_state", "start_autogoto", "get_center_state",
    # solve
    "get_solve_result", "get_last_solve_result", "get_annotate_result",
    "solve", "start_solve", "get_solve_state", "get_platesolve_result",
    # guider
    "get_guider_state", "get_guide_state", "guide_get_state",
    "get_guide_setting", "get_guiding_state", "get_dither_setting",
    "get_calibration_state", "get_guide_star", "get_guider_info",
    "get_guide_result", "get_guide_data", "get_guide_graph",
    # align / polar
    "get_align_state", "get_polar_align_state", "get_pa_state",
    "get_paa_result", "get_polar_align_result",
    # autorun / plan / focus
    "get_autorun_state", "get_plan_state", "get_plan", "get_run_state",
    "get_autofocus_state", "get_autofocuse_state", "get_focus_state",
    "get_af_result", "get_auto_focuse_state",
    # system
    "get_dew_heater", "get_heater", "get_pi_status", "get_pistatus",
    "pi_get_info", "get_battery", "get_power", "get_ap", "get_wifi",
    "get_time", "get_gps", "get_location", "get_site", "get_history",
    "get_current_img", "get_preview", "get_thumbnail", "get_capture_state",
    "get_exposure_state", "get_work_state", "get_state",
    "get_version", "get_svr_version", "get_focal_length", "get_lens",
]


# Connect/scan/introspection candidates — how a mount would attach, and whether
# the server exposes a method list. All read/query or connection-scan verbs.
SPECIAL = [
    "get_connected_devices", "get_connect_state", "scan_mount", "scan_air",
    "open_mount", "connect_mount", "get_mount", "mount_scan", "mount_state",
    "get_device_list", "get_device_names", "get_all_state",
    "list_methods", "get_methods", "get_method_list", "help", "get_api",
    "system.listMethods", "rpc.discover", "get_rpc_list", "get_cmd_list",
    "MountGetEquCoord", "GetMountState", "get_EQ_coord", "get_eq_coord",
    "get_ra", "get_dec", "get_azalt", "get_alt_az", "get_pointing",
]


def build_wordlist(curated_only=False):
    names = set(CURATED) | set(SPECIAL)
    if not curated_only:
        for d in DEVICES:
            for a in ATTRS:
                names.add(f"get_{d}_{a}")
                names.add(f"{d}_get_{a}")
    return sorted(names)


def classify(reply):
    """Return ('missing'|'exists'|'silent', detail-string)."""
    if reply is None:
        return "silent", ""
    if "result" in reply:
        return "exists", "OK  " + json.dumps(reply["result"], ensure_ascii=False)[:150]
    err = reply.get("error")
    code = reply.get("code")
    if code == 103 or err == "method not found":
        return "missing", ""
    return "exists", f"err[{code}] {err}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--key", default="embedded_key.pem")
    ap.add_argument("--wait", type=float, default=12.0)
    ap.add_argument("--curated-only", action="store_true",
                    help="skip the get_<device>_<attr> combos, test only the "
                         "curated + special lists")
    add_log_args(ap)
    a = ap.parse_args()
    configure_logging(a)

    words = build_wordlist(curated_only=a.curated_only)
    air = Air(a.host, 4700, key=a.key)
    if not air.verified:
        print("auth failed — cannot probe")
        return 1
    print(f"authenticated. probing {len(words)} read-only candidate names\n")

    slots = []
    with log.slow("firing %d probes" % len(words), quiet_for=1.0,
                  detail=lambda: "%d/%d sent" % (len(slots), len(words))):
        for m in words:
            slots.append((m, air.send(m, [])))
            time.sleep(0.015)
    # A fixed collection window — report how it fills rather than going quiet.
    with log.slow("collecting replies (%.0fs window)" % a.wait, quiet_for=0.0,
                  detail=lambda: "%d/%d answered"
                                 % (sum(1 for _, (_, sl) in slots if sl[0].is_set()),
                                    len(slots))):
        time.sleep(a.wait)

    exists, missing, silent = [], [], []
    for m, (rid, slot) in slots:
        reply = slot[1] if slot[0].is_set() else None
        kind, detail = classify(reply)
        if kind == "exists":
            exists.append((m, detail))
        elif kind == "silent":
            silent.append(m)
        else:
            missing.append(m)

    print(f"=== {len(exists)} EXIST ===")
    for m, d in sorted(exists):
        print(f"  {m:<28} {d}")
    if silent:
        print(f"\n{len(silent)} silent (no reply): {', '.join(sorted(silent))}")
    log.info("%d exist, %d silent, %d not found (code 103)",
             len(exists), len(silent), len(missing))
    print(f"\n{len(missing)} not found (code 103), hidden.")
    air.close()


if __name__ == "__main__":
    raise SystemExit(main())
