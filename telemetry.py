#!/usr/bin/env python3
"""Log the rig's power telemetry to CSV, so a flat battery stops looking like a crash.

From the network side a dead battery is indistinguishable from a crashed Air:
no reply to ping, the ARP entry goes `(incomplete)`, every port closed, and a
full subnet sweep finds nothing. Both signals that precede it are already being
broadcast, so this records them:

    4700  get_power_supply               [[volts, amps]] — the primary source
    4400  scope_get_info.input_voltage   fallback, in millivolts
    4700  PiStatus events                is_undervolt / is_over_current / temp

**Prefer `get_power_supply`.** It works with nothing attached, whereas
`scope_get_info` needs the mount connected and answers `mount is not connected`
otherwise — which is the state after every Air restart, and precisely when you
are trying to work out whether the battery died. The mount reading is kept as a
fallback and cross-check. Treat the **voltage** as the trustworthy half of the
pair: the current figure looks like one rail rather than total draw
(13.2 V x 0.17 A = 2.2 W is far too low for the Air alone).

`is_undervolt` is the Air complaining its own supply has sagged, which is the
earlier of the two warnings. PiStatus needs the 4700 handshake, so pass --key
for it; without a key the voltage half still works on its own.

PiStatus cannot be requested — it is broadcast, and the cadence **varies with
how busy the Air is**: measured at 2-4 s during an autorun but ~20 s idle. The
polling loop picks one up between rows at any sensible interval; `--once` waits
up to 25 s for one and says so if none arrives.

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

from air_rpc import Air

MOUNT_PORT = 4400
MAIN_PORT = 4700
KEEPALIVE_S = 4.0          # both ports drop an idle socket after ~15s
FIELDS = ["time", "volts", "amps", "input_mv", "undervolt", "overcurrent",
          "pi_temp_c", "alt", "az", "tracking", "note"]


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
            return True
        except Exception:
            self.air = None
            return False

    def call(self, method, params=None, timeout=8):
        for attempt in range(2):
            if self.air is None and not self.connect():
                return None
            try:
                r = self.air.call(method, params or [], timeout=timeout)
                return r.get("result") if isinstance(r, dict) else None
            except Exception:
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


def sample(mount, main, last_pi, wait_pi=0.0):
    """One reading. Returns (row_dict, updated_last_pi).

    `wait_pi` seconds to block waiting for a first PiStatus (see below)."""
    row = {k: "" for k in FIELDS}
    row["time"] = datetime.datetime.now().isoformat(timespec="seconds")
    reached = False

    # Primary: works with nothing attached, unlike the mount's own reading.
    if main is not None:
        ps = main.call("get_power_supply")
        if isinstance(ps, list) and ps and isinstance(ps[0], list) and len(ps[0]) >= 2:
            row["volts"] = f"{float(ps[0][0]):.4f}"
            row["amps"] = f"{float(ps[0][1]):.4f}"
            reached = True

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
        reached = True
    elif not reached:
        row["note"] = "unreachable"
    else:
        row["note"] = "mount detached"      # 4700 answered, so the Air is alive

    if main is not None:
        for e in main.events():
            if e.get("Event") == "PiStatus":
                last_pi = e
        # PiStatus cannot be requested, and its cadence varies with load —
        # 2-4s during an autorun, ~20s idle. On the first sample nothing has
        # arrived yet, so without a wait a --once run always reports a blank
        # undervolt, the one field most worth having.
        if last_pi is None and wait_pi > 0:
            deadline = time.time() + wait_pi
            while last_pi is None and time.time() < deadline:
                time.sleep(0.3)
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
    if row["volts"] != "":
        volts = f"{float(row['volts']):.2f} V"
        if row["amps"] != "":
            volts += f"  {float(row['amps']):.2f} A"
    elif row["input_mv"] != "":
        volts = f"{int(row['input_mv'])/1000:.2f} V (mount)"
    else:
        return f"{row['time']}  --  {row['note'] or 'no reading'}"
    bits = [f"{row['time']}  {volts}"]
    if row["undervolt"] != "":
        bits.append("UNDERVOLT" if row["undervolt"] == 1 else "ok")
    if row["overcurrent"] == 1:
        bits.append("OVERCURRENT")
    if row["pi_temp_c"] != "":
        bits.append(f"pi {row['pi_temp_c']}C")
    if row["alt"] != "":
        bits.append(f"alt {row['alt']}")
    if row["note"]:
        bits.append(f"[{row['note']}]")
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
    a = ap.parse_args()

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
                    row, last_pi = sample(mount, main_link, last_pi,
                                          wait_pi=25.0 if a.once else 0.0)
                    w.writerow(row); fh.flush()
                    print(describe(row), flush=True)
                    if a.once:
                        if main_link is not None and row["undervolt"] == "":
                            print("  (no PiStatus within 25s — undervolt not sampled)",
                                  file=sys.stderr)
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
