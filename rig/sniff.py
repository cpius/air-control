#!/usr/bin/env python3
"""Watch every event the Air broadcasts, across all four channels at once.

The Air fans its events out to *every* connected client, so running this while
you drive the rig from the phone shows what the app's actions actually produce
-- without needing to intercept the phone's own packets. That is how the autorun
protocol was worked out, and it is the way to compare a command that works from
the app against the same command failing from here.

Channels: 4400 mount+guide (no auth), 4700 main (RSA handshake), 4800 images,
4900 system/usage -- the last one is the only place plain-language `fail_reason`
strings turn up.

Both 4400 and 4700 drop sockets that sit idle, so each carries a keepalive; a
sniffer without one just watches itself get disconnected every 10-15 s.

    python3 sniff.py                       # all channels, 5 minutes
    python3 sniff.py --seconds 120
    python3 sniff.py --all                 # do not filter the routine chatter
    python3 sniff.py --grep ScopeGoto      # only lines matching
"""
import argparse, json, os, socket, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from air_rpc import Air
from airlog import add_log_args, configure_logging, get_logger

log = get_logger("sniff")
KEY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "embedded_key.pem")

# Routine chatter that would bury anything interesting.
NOISE = {"PiStatus", "Temperature", "Version", "GuideStep", "AutoGoto_Step_Info"}
# Events worth shouting about.
LOUD = ("ScopeGoto", "ScopeHome", "AutoGoto", "PlateSolve", "fail_reason",
        "route", "error", "Annotate")


class Sniffer:
    def __init__(self, host, seconds, show_all, grep):
        self.host, self.seconds = host, seconds
        self.show_all, self.grep = show_all, grep
        self.t0 = time.time()
        self.lock = threading.Lock()

    def left(self):
        return self.seconds - (time.time() - self.t0)

    def out(self, tag, msg):
        if self.grep and self.grep.lower() not in msg.lower():
            return
        mark = "  <<<" if any(k in msg for k in LOUD) else ""
        with self.lock:
            print(f"[{time.time()-self.t0:7.2f}s {tag:12s}] {msg[:700]}{mark}", flush=True)

    def interesting(self, txt):
        if self.show_all:
            return True
        try:
            ev = json.loads(txt).get("Event")
        except Exception:
            return True
        return ev not in NOISE

    def raw(self, port, label, keepalive_json=None):
        """Plain newline-JSON channel. keepalive_json is sent every 5s if given."""
        while self.left() > 0:
            try:
                s = socket.create_connection((self.host, port), timeout=6)
                s.settimeout(1.0)
                self.out(label, "-- connected --")
                buf, last = b"", time.time()
                while self.left() > 0:
                    if keepalive_json and time.time() - last > 5.0:
                        try:
                            s.sendall((json.dumps(keepalive_json) + "\r\n").encode())
                        except Exception:
                            break
                        last = time.time()
                    try:
                        d = s.recv(65536)
                    except socket.timeout:
                        continue
                    except Exception:
                        break
                    if not d:
                        break
                    buf += d
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        txt = line.decode("utf8", "replace")
                        if self.interesting(txt):
                            self.out(label, txt)
                s.close()
            except Exception as e:
                if self.left() > 0:
                    self.out(label, f"-- reconnecting ({type(e).__name__}) --")
                    time.sleep(1.5)

    def main_channel(self, key):
        """4700 needs the RSA handshake before it streams the gated events."""
        while self.left() > 0:
            try:
                a = Air(self.host, 4700, timeout=8, key=key)
                self.out("4700/main", "-- handshake ok --")
                last = time.time()
                while self.left() > 0:
                    try:
                        ev = a.events.get(timeout=1.0)
                        txt = json.dumps(ev)
                        if self.interesting(txt):
                            self.out("4700/main", txt)
                    except Exception:
                        pass
                    if time.time() - last > 5.0:      # keepalive: 4700 drops idlers
                        try:
                            a.call("test_connection", [], timeout=5)
                        except Exception:
                            break
                        last = time.time()
                a.close()
            except Exception as e:
                if self.left() > 0:
                    self.out("4700/main", f"-- reconnecting ({type(e).__name__}) --")
                    time.sleep(2)

    def run(self, key):
        for port, label, ka in ((4400, "4400/mount", {"id": 999, "method": "scope_get_info", "params": []}),
                                (4900, "4900/system", None),
                                (4800, "4800/image", None)):
            threading.Thread(target=self.raw, args=(port, label, ka), daemon=True).start()
        threading.Thread(target=self.main_channel, args=(key,), daemon=True).start()
        print(f"listening for {self.seconds}s -- drive the rig from the app now\n", flush=True)
        try:
            while self.left() > 0:
                time.sleep(0.3)
        except KeyboardInterrupt:
            pass
        print("\n-- done --", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ)
    ap.add_argument("--key", default=KEY)
    ap.add_argument("--seconds", type=int, default=300)
    ap.add_argument("--all", action="store_true", help="do not filter routine chatter")
    ap.add_argument("--grep", help="only show lines containing this string")
    add_log_args(ap)
    a = ap.parse_args()
    configure_logging(a)
    Sniffer(a.host, a.seconds, a.all, a.grep).run(a.key)


if __name__ == "__main__":
    main()
