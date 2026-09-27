#!/usr/bin/env python3
"""Log the rig's power telemetry to CSV, so a flat battery stops looking like a crash.

From the network side a dead battery is indistinguishable from a crashed Air:
no reply to ping, the ARP entry goes `(incomplete)`, every port closed, and a
full subnet sweep finds nothing. Both signals that precede it are already being
broadcast, so this records them:

    4400  scope_get_info.input_voltage   supply to the AM5N, in millivolts
    4700  PiStatus events                is_undervolt / is_over_current / temp

`is_undervolt` is the Air complaining its own supply has sagged, which is the
earlier of the two warnings. PiStatus needs the 4700 handshake, so pass --key
for it; without a key the voltage half still works on its own.

**The rows that matter most are the ones where the read FAILS.** A poll that
times out is written as a row with an empty voltage and `note=unreachable`,
so the log captures the moment power went away rather than simply stopping.

Rows append to <dir>/power-YYYY-MM-DD.csv (one file per night, named for the
local date at start), so a voltage curve accumulates across sessions.

    python3 telemetry.py --host <air-ip>
    python3 telemetry.py --host <air-ip> --key embedded_key.pem
    python3 telemetry.py --host <air-ip> --interval 30 --dir ~/ASICAP/telemetry
    python3 telemetry.py --host <air-ip> --once        # single reading, then exit

Open question this exists to answer: does the voltage sag gradually (a real
fuel gauge, warn on a threshold) or hold flat and then collapse (a regulated
output, in which case only is_undervolt gives any notice)? One full
run-to-flat settles it. Until then, do not read a single voltage as
"percentage remaining".
"""

import argparse
import csv
import datetime
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from air_rpc import Air
from airlog import add_log_args, configure_logging, get_logger

log = get_logger("telemetry")

MOUNT_PORT = 4400
MAIN_PORT = 4700
KEEPALIVE_S = 4.0          # both ports drop an idle socket after ~15s
FIELDS = ["time", "input_mv", "undervolt", "overcurrent", "pi_temp_c",
          "alt", "az", "tracking", "note"]


class Link:
    """One reconnecting RPC connection. Never raises at the call site: a dead
    link returns None, which the caller logs rather than crashing on — losing
    the connection is the event we are here to record."""

    def __init__(self, host, port, key=None):
        self.host, self.port, self.key = host, port, key
        self.air = None
        self.connect()

    def connect(self):
        try:
            self.air = Air(self.host, self.port, timeout=8,
                           key=self.key if self.port == MAIN_PORT else None)
            log.info("connected to %s:%d", self.host, self.port)
            return True
        except Exception as e:
            # This is a data point, not an error to swallow: losing the link is
            # the event this whole script exists to timestamp.
            log.warn("cannot reach %s:%d — %s", self.host, self.port, e)
            self.air = None
            return False

    def call(self, method, params=None, timeout=8):
        for attempt in range(2):
            if self.air is None and not self.connect():
                return None
            try:
                r = self.air.call(method, params or [], timeout=timeout)
                return r.get("result") if isinstance(r, dict) else None
            except Exception as e:
                log.warn("%s on %d failed (%s) — %s", method, self.port, e,
                         "giving up for this row" if attempt else "reconnecting")
                self.close()
                if attempt:
                    return None
        return None

    def events(self):
        if self.air is None:
            return []
        try:
            return self.air.drain_events()
        except Exception:
            self.close()
            return []

    def close(self):
        try:
            if self.air:
                self.air.close()
        except Exception:
            pass
        self.air = None


def sample(mount, main, last_pi):
    """One reading. Returns (row_dict, updated_last_pi)."""
    row = {k: "" for k in FIELDS}
    row["time"] = datetime.datetime.now().isoformat(timespec="seconds")

    st = mount.call("scope_get_info")
    if isinstance(st, dict):
        mv = st.get("input_voltage")
        row["input_mv"] = mv if mv is not None else ""
        for key, col in (("Alt", "alt"), ("Az", "az")):
            v = st.get(key)
            if isinstance(v, (int, float)):
                row[col] = f"{v:.2f}"
        trk = st.get("is_enable_track")
        row["tracking"] = "" if trk is None else int(bool(trk))
    else:
        row["note"] = "unreachable"
        log.error("scope_get_info returned nothing — the mount link is down. "
                  "This row IS the record of when power went away.")

    if main is not None:
        for e in main.events():
            if e.get("Event") == "PiStatus":
                last_pi = e
        main.call("test_connection")          # keepalive; events need no request
        if last_pi:
            row["undervolt"] = int(bool(last_pi.get("is_undervolt")))
            row["overcurrent"] = int(bool(last_pi.get("is_over_current")))
            t = last_pi.get("temp")
            if isinstance(t, (int, float)):
                row["pi_temp_c"] = f"{t:.1f}"
    return row, last_pi


def describe(row):
    mv = row["input_mv"]
    if mv == "":
        return f"{row['time']}  --  {row['note'] or 'no reading'}"
    volts = f"{int(mv)/1000:.2f} V"
    bits = [f"{row['time']}  {volts}"]
    if row["undervolt"] != "":
        bits.append("UNDERVOLT" if row["undervolt"] == 1 else "ok")
    if row["overcurrent"] == 1:
        bits.append("OVERCURRENT")
    if row["pi_temp_c"] != "":
        bits.append(f"pi {row['pi_temp_c']}C")
    if row["alt"] != "":
        bits.append(f"alt {row['alt']}")
    return "  ".join(bits)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--key", help="RSA key for 4700; without it PiStatus "
                                  "(undervolt/temp) is not logged")
    ap.add_argument("--interval", type=float, default=60.0,
                    help="seconds between rows (default 60)")
    ap.add_argument("--dir", default=os.path.expanduser("~/ASICAP/telemetry"),
                    help="where to append power-YYYY-MM-DD.csv")
    ap.add_argument("--once", action="store_true", help="one reading, then exit")
    add_log_args(ap)
    a = ap.parse_args()
    configure_logging(a)

    os.makedirs(a.dir, exist_ok=True)
    path = os.path.join(a.dir, f"power-{datetime.date.today():%Y-%m-%d}.csv")
    fresh = not os.path.exists(path) or os.path.getsize(path) == 0

    mount = Link(a.host, MOUNT_PORT)
    main_link = Link(a.host, MAIN_PORT, key=a.key) if a.key else None
    if a.key and main_link.air is None:
        print("warning: 4700 unavailable — logging voltage only", file=sys.stderr)

    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if fresh:
            w.writeheader()
        print(f"logging to {path} every {a.interval:.0f}s "
              f"(Ctrl-C to stop){' — single reading' if a.once else ''}")
        last_pi, next_row = None, 0.0
        try:
            while True:
                now = time.time()
                if now >= next_row:
                    row, last_pi = sample(mount, main_link, last_pi)
                    w.writerow(row)
                    fh.flush()
                    os.fsync(fh.fileno())   # the interesting rows are the last ones
                    if row["undervolt"] == 1:
                        log.error("UNDERVOLT — the Air says its own supply has "
                                  "sagged. This is the earlier of the two warnings.")
                    elif row["note"]:
                        log.warn("row logged with note=%s", row["note"])
                    else:
                        log.info("%s", describe(row))
                    print(describe(row), flush=True)
                    if a.once:
                        break
                    next_row = now + a.interval
                # poll faster than the row interval so the sockets stay alive
                time.sleep(min(KEEPALIVE_S, max(0.2, next_row - time.time())))
        except KeyboardInterrupt:
            print("\nstopped")
        finally:
            mount.close()
            if main_link:
                main_link.close()


if __name__ == "__main__":
    sys.exit(main())
