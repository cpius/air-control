#!/usr/bin/env python3
"""Raw JSON-RPC console for the ASIAIR firmware channel on TCP 4700.

This is the undocumented channel the ASIAIR phone app itself uses. Alpaca
(see alpaca.py) covers the camera properly; this one is how you reach the
things Alpaca doesn't expose — mount goto, plate solving, autorun, guiding.

Framing: newline-terminated JSON, request/response plus unsolicited events.
    -> {"id":1,"method":"test_connection","params":[]}\\n
    <- {"id":1,"jsonrpc":"2.0","Timestamp":"...","result":...}\\n
    <- {"Event":"...","Timestamp":"..."}            (async, unprompted)

Auth (firmware 7.18+): most methods are dropped in silence until the connection
completes an RSA challenge handshake. Pass --key (or Air(host, key=...)) to run
it: get_verify_str returns a challenge, we sign it RSA PKCS#1 v1.5 / SHA-1 and
call verify_client([signature, challenge]); pi_is_verified then reads True and
the gated methods answer. Signing needs the `cryptography` package (imported
lazily, so the unauthenticated paths stay stdlib-only).

Usage:
    python3 air_rpc.py --host <air-ip> probe                 # API surface
    python3 air_rpc.py --host <air-ip> --key embedded_key.pem probe
    python3 air_rpc.py --host <air-ip> --key embedded_key.pem call get_device_state
    python3 air_rpc.py --host <air-ip> --key embedded_key.pem console
    python3 air_rpc.py --host <air-ip> listen                # watch events
"""

import argparse
import base64
import json
import os
import queue
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from airlog import add_log_args, configure_logging, get_logger

log = get_logger("rpc")

PORT = 4700

# Anything past this is "slow" and gets a line a second while it runs. Every
# call here crosses the network to a Raspberry Pi that is also running a camera,
# so multi-second replies are routine and a silent wait is indistinguishable
# from a dead socket -- which is precisely the failure this logging exists for.
SLOW_CALL_S = 1.0

# Method names lifted from seestar_alp — Seestar and ASIAIR share ZWO's RPC
# framework, so this is a high-quality wordlist rather than guesswork. The
# device answers what it implements and stays *silent* on what it doesn't,
# so probing is "send everything, see who replies".
#
# Read-only only. Anything that moves the mount, exposes, or reboots lives in
# WRITE_METHODS below and is never sent by `probe`.
PROBE_METHODS = [
    "test_connection", "noop",
    "get_app_setting", "get_setting", "get_test_setting",
    "get_device_state", "get_event_state", "get_view_state",
    "get_camera_info", "get_camera_state", "get_camera_exp_and_bin",
    "get_control_value", "get_controls",
    "get_disk_volume", "get_image_save_path", "get_img_name_field",
    "get_albums", "is_stacked", "get_stack_info", "get_stack_setting",
    "get_sequence_setting", "get_sensor_calibration",
    "get_solve_result", "get_last_solve_result", "get_annotate_result",
    "get_focuser_position", "get_wheel_position", "get_wheel_setting",
    "get_wheel_state", "get_stacked_img", "get_server_log",
    "get_user_location", "get_verify_str", "pi_is_verified",
    "pi_get_ap", "pi_get_time", "pi_station_state",
    "scope_get_equ_coord", "scope_get_horiz_coord", "scope_get_ra_dec",
    "scope_get_track_state",
    "iscope_get_app_state", "scan_iscope",
]

# NOT probed — these change state. Call them deliberately via `call`/`console`.
WRITE_METHODS = [
    "scope_goto", "scope_sync", "scope_park", "scope_speed_move",
    "scope_move_to_horizon", "scope_set_track_state",
    "start_exposure", "stop_exposure", "start_solve", "start_polar_align",
    "stop_polar_align", "start_auto_focuse", "stop_auto_focuse",
    "start_create_dark", "start_scan_planet", "move_focuser",
    "begin_streaming", "stop_streaming",
    "iscope_start_view", "iscope_stop_view", "iscope_start_stack",
    "set_setting", "set_app_setting", "set_control_value", "set_stack_setting",
    "set_stack_type", "set_sequence_setting", "set_user_location",
    "set_img_name_field", "set_sensor_calibration", "set_wheel_position",
    "verify_client", "play_sound",
    "pi_output_set2", "pi_set_time", "pi_reboot", "pi_shutdown",
]


def sign_challenge(key_path, challenge):
    """Sign a challenge string RSA PKCS#1 v1.5 / SHA-1, base64-encoded.

    `cryptography` is imported here rather than at module top so the rest of
    the module keeps working (unauthenticated) without it installed.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    with log.slow("sign challenge (RSA/SHA-1)", quiet_for=0.5):
        with open(key_path, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
        log.debug("loaded %d-bit key from %s", key.key_size, key_path)
        sig = key.sign(challenge.encode(), padding.PKCS1v15(), hashes.SHA1())
    return base64.b64encode(sig).decode()


class Air:
    def __init__(self, host, port=PORT, timeout=10, key=None):
        self.host, self.port = host, port
        # A TCP connect to an Air that has lost power does not refuse, it hangs
        # until the timeout -- so tick through it rather than going quiet.
        with log.slow("connect %s:%d" % (host, port), quiet_for=1.0):
            self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(None)
        log.debug("connected to %s:%d (timeout %ss)", host, port, timeout)
        self._id = 0
        self._buf = b""
        self._replies = {}
        self._events = queue.Queue()
        self._lock = threading.Lock()
        self._alive = True
        self.verified = False
        # Counters live before the reader starts: it logs them on socket close,
        # and a connection that dies instantly would otherwise race the setup.
        self._n_calls = 0
        self._n_events = 0
        self._closing = False
        self._rx = threading.Thread(target=self._reader, daemon=True)
        self._rx.start()
        if key:
            self.verify(key)

    @property
    def alive(self):
        """False once the reader thread has seen the socket close.

        Worth checking in any long wait: 4400 hangs up a connection that has
        been idle ~15s, and on a dead socket a quiet event queue is
        indistinguishable from a slew still in progress.
        """
        return self._alive

    def verify(self, key_path, timeout=8):
        """Complete the RSA challenge handshake; unlocks the gated methods.

        Returns True when pi_is_verified confirms. Raises on a missing/rejected
        challenge. Verification is per-connection — it lives on this socket.
        """
        with log.slow("RSA handshake on %d" % self.port) as tk:
            tk.set("get_verify_str")
            vs = self.call("get_verify_str", [], timeout=timeout)
            challenge = (vs.get("result") or {}).get("str") if isinstance(vs, dict) else None
            if not challenge:
                raise RuntimeError(f"no challenge from get_verify_str: {vs}")
            log.debug("challenge: %s", challenge)
            tk.set("signing")
            signature = sign_challenge(key_path, challenge)
            # verify_client takes two positional strings: [signature, challenge].
            tk.set("verify_client")
            self.call("verify_client", [signature, challenge], timeout=timeout)
            tk.set("pi_is_verified")
            pv = self.call("pi_is_verified", [], timeout=timeout)
            self.verified = bool(isinstance(pv, dict) and pv.get("result"))
        if self.verified:
            log.info("4700 handshake OK — gated methods unlocked")
        else:
            log.warn("handshake completed but pi_is_verified says False: %r", pv)
        return self.verified

    def _reader(self):
        while self._alive:
            try:
                chunk = self.sock.recv(65536)
            except OSError as e:
                log.debug("reader on %d: socket error %s", self.port, e)
                break
            if not chunk:
                log.debug("reader on %d: peer closed the connection", self.port)
                break
            self._buf += chunk
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    self._events.put({"_raw": line[:500].decode("utf-8", "replace")})
                    continue
                if isinstance(msg, dict) and "id" in msg:
                    with self._lock:
                        ev = self._replies.get(msg["id"])
                        if ev:
                            ev[1] = msg
                            ev[0].set()
                            continue
                self._n_events += 1
                # Events are the Air's only progress channel for slews, solves
                # and exposures, so every one is logged at debug.
                if isinstance(msg, dict):
                    log.debug("event %-14s %s", msg.get("Event", "?"),
                              json.dumps({k: v for k, v in msg.items()
                                          if k not in ("Event", "Timestamp")},
                                         ensure_ascii=False)[:200])
                self._events.put(msg)
        self._alive = False
        # A close WE asked for is unremarkable; one the Air did is a finding --
        # it is how an idle-timeout drop or a power loss shows up.
        if self._closing:
            log.debug("reader on %d finished (%d calls, %d events)",
                      self.port, self._n_calls, self._n_events)
        else:
            log.info("the Air closed %s:%d (%d calls, %d events)",
                     self.host, self.port, self._n_calls, self._n_events)

    def send(self, method, params=None):
        """Fire a request without waiting. Returns (rid, slot) for later collection."""
        with self._lock:
            self._id += 1
            rid = self._id
            slot = [threading.Event(), None]
            self._replies[rid] = slot
        req = {"id": rid, "method": method}
        if params is not None:
            req["params"] = params
        log.trace("-> #%d %s %s", rid, method,
                  json.dumps(params, ensure_ascii=False)[:160] if params else "")
        self.sock.sendall((json.dumps(req) + "\r\n").encode())
        return rid, slot

    def call(self, method, params=None, timeout=15):
        """Send and block for the reply, ticking once a second while it waits.

        The tick is the whole point: a reply that never comes and a reply that
        takes 12 s look identical from here, and several methods (open_camera,
        start_solve, set_connected) routinely take many seconds. The ticker
        thread keeps reporting even though this thread is parked in Event.wait.
        """
        t0 = time.time()
        self._n_calls += 1
        rid, slot = self.send(method, params)
        with log.slow("%s on %d" % (method, self.port), quiet_for=SLOW_CALL_S,
                      detail=lambda: "waiting for reply #%d (timeout %.0fs)"
                                     % (rid, timeout)):
            got = slot[0].wait(timeout)
        with self._lock:
            self._replies.pop(rid, None)
        dt = time.time() - t0
        if not got:
            log.warn("no reply to %r on %d within %ss", method, self.port, timeout)
            raise TimeoutError(f"no reply to {method!r} within {timeout}s")
        rep = slot[1]
        if isinstance(rep, dict) and rep.get("error"):
            log.debug("<- #%d %s FAILED in %.2fs: %s (code %s)", rid, method, dt,
                      rep.get("error"), rep.get("code"))
        else:
            log.trace("<- #%d %s in %.2fs: %s", rid, method, dt,
                      json.dumps(rep.get("result") if isinstance(rep, dict) else rep,
                                 ensure_ascii=False)[:200])
        return rep

    def drain_events(self):
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    def close(self):
        if self._alive:
            log.debug("closing %s:%d after %d call(s)", self.host, self.port,
                      self._n_calls)
        self._closing = True
        self._alive = False
        try:
            self.sock.close()
        except OSError:
            pass
        # Join the reader before returning. It writes its own closing line, and
        # a daemon thread that reaches the logger *during interpreter shutdown*
        # takes the whole process down with a fatal "runtime state: finalizing"
        # error -- which is exactly what happens when close() is the last thing
        # a script does. Joining costs nothing and removes the race.
        rx = getattr(self, "_rx", None)
        if rx is not None and rx is not threading.current_thread():
            rx.join(timeout=1.0)


def show(msg):
    print(json.dumps(msg, indent=2, ensure_ascii=False))


def cmd_probe(air, wait=10.0):
    """Pipelined: fire every candidate, then collect. Silence == unimplemented."""
    print(f"probing {len(PROBE_METHODS)} read-only methods "
          f"(pipelined, {wait:.0f}s collection window)\n")
    slots = []
    with log.slow("firing %d probes" % len(PROBE_METHODS),
                  detail=lambda: "%d/%d sent" % (len(slots), len(PROBE_METHODS))):
        for m in PROBE_METHODS:
            slots.append((m, air.send(m, [])))
            time.sleep(0.02)

    # A fixed collection window: report how many have answered as it drains.
    with log.slow("collecting replies (%.0fs window)" % wait, quiet_for=0.0,
                  detail=lambda: "%d/%d answered"
                                 % (sum(1 for _, (_, sl) in slots if sl[0].is_set()),
                                    len(slots))):
        time.sleep(wait)

    live, silent = [], []
    for m, (rid, slot) in slots:
        if not slot[0].is_set():
            silent.append(m)
            continue
        r = slot[1]
        if "result" in r:
            live.append(m)
            print(f"  {m:<26} OK    {json.dumps(r['result'], ensure_ascii=False)[:170]}")
        else:
            print(f"  {m:<26} err   {json.dumps(r, ensure_ascii=False)[:120]}")

    print(f"\n{len(live)} implemented | {len(silent)} silent")
    if silent:
        print("silent: " + ", ".join(silent))
    ev = air.drain_events()
    if ev:
        kinds = {}
        for e in ev:
            kinds.setdefault(e.get("Event", "?"), []).append(e)
        print(f"\n{len(ev)} async events, by type:")
        for k, v in kinds.items():
            print(f"  {k:<16} x{len(v):<4} {json.dumps(v[-1], ensure_ascii=False)[:150]}")


def cmd_listen(air, seconds):
    print(f"listening for events for {seconds}s (Ctrl-C to stop)\n")
    end = time.time() + seconds
    n = 0
    last = [time.time()]
    try:
        # A quiet event channel is a real observation (nothing is running, or
        # the socket died) so the ticker reports the silence, once a second.
        with log.slow("listening", quiet_for=1.0,
                      detail=lambda: "%d event(s), %.0fs since the last one%s"
                                     % (n, time.time() - last[0],
                                        "" if air.alive else "  SOCKET CLOSED")):
            while time.time() < end:
                for e in air.drain_events():
                    n += 1
                    last[0] = time.time()
                    print(time.strftime("%H:%M:%S"), json.dumps(e, ensure_ascii=False))
                time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    log.info("listened %.0fs, saw %d event(s)", seconds, n)


def cmd_console(air):
    print("ASIAIR RPC console.  <method> [json-params]   |   .events   .quit\n")
    while True:
        try:
            line = input("air> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in (".quit", ".q", "exit"):
            return
        if line == ".events":
            for e in air.drain_events():
                print("  ", json.dumps(e, ensure_ascii=False))
            continue
        method, _, rest = line.partition(" ")
        params = []
        if rest.strip():
            try:
                params = json.loads(rest)
            except ValueError:
                print("  params must be JSON, e.g.  set_setting [{\"exp_ms\":1000}]")
                continue
        try:
            show(air.call(method, params))
        except TimeoutError as e:
            print("  ", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--key", help="RSA key PEM; runs the auth handshake to "
                                  "unlock gated methods (mount, solve, autorun)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    sub.add_parser("console")
    l = sub.add_parser("listen"); l.add_argument("--seconds", type=int, default=60)
    c = sub.add_parser("call")
    c.add_argument("method"); c.add_argument("params", nargs="?", default="[]")

    add_log_args(ap)          # after the subparsers, so `call foo -v` works too
    a = ap.parse_args()
    configure_logging(a)
    log.info("air_rpc %s -> %s:%d", a.cmd, a.host, a.port)
    try:
        air = Air(a.host, a.port)
    except OSError as e:
        print(f"cannot connect to {a.host}:{a.port} — {e}", file=sys.stderr)
        return 1

    if a.key:
        try:
            ok = air.verify(a.key)
        except Exception as e:  # missing key, no cryptography, rejected challenge
            print(f"auth failed: {e}", file=sys.stderr)
            air.close()
            return 1
        print(f"auth: verified={ok}", file=sys.stderr)
        if not ok:
            air.close()
            return 1

    try:
        if a.cmd == "probe":
            cmd_probe(air)
        elif a.cmd == "console":
            cmd_console(air)
        elif a.cmd == "listen":
            cmd_listen(air, a.seconds)
        elif a.cmd == "call":
            show(air.call(a.method, json.loads(a.params)))
    finally:
        air.close()


if __name__ == "__main__":
    sys.exit(main())
