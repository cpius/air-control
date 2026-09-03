#!/usr/bin/env python3
"""Every command out and every message in, on one timeline, for the dashboard.

`air_rpc.Air` is the single chokepoint for all four channels -- 4400 (mount and
guide), 4700 (camera, focuser, solver), and by extension everything the 13 tools
in this directory do. Taping three points inside it therefore instruments the
whole toolkit at once, with no change to any caller:

    Air.send      -> cmd     one line per outgoing request
    Air.call      -> reply   the matching answer, with its round-trip time
    Air._reader   -> event   the Air talking unprompted

Output is JSON Lines, one file per session, named for the LOCAL date at start
(matching telemetry.py, and for the same reason: a session that crosses midnight
belongs to the evening it began). Lines are flushed but not fsync'd -- guide
steps arrive several a second and an fsync each would be felt.

**Attribution is the point.** A raw event stream is unreadable: `GuideStep` at
2 Hz buries the one `AutoGotoStep` that says the slew failed. Every record is
therefore tagged with the subsystem it belongs to -- mount, guide, main camera,
focuser, solver, system -- derived from the method name for commands and the
event name for events, with the port as the tiebreak. `DEVICES` below is the
whole ruleset and is meant to be edited as new event names turn up; anything
unrecognised lands in `other` rather than being silently dropped, so an
unclassified event is visible in the dashboard instead of invisible.

Recording is ON by default whenever anything imports air_rpc. Set
ASICAP_DASHBOARD=0 to turn it off, or ASICAP_DASHBOARD_DIR to move it.

    from recorder import rec
    rec.note("starting focus sweep", device="focuser")
    rec.artifact("preview", "/path/to/preview.jpg", meta={"exposure": 10})
"""

import datetime
import json
import os
import threading
import time

DEFAULT_DIR = os.path.expanduser("~/ASICAP/dashboard/data")

# Values longer than this are truncated in the log. Solve results and device
# state blobs run to several KB, and an image payload would be megabytes; the
# dashboard wants the shape of the reply, not a byte-exact copy.
MAX_VALUE_CHARS = 1200

# Ports, for the tiebreak when a name says nothing.
PORT_HINT = {4400: "mount", 4700: "camera", 4800: "camera", 4900: "system"}

# ---------------------------------------------------------------------------
# Attribution. Ordered: the first rule that matches wins, so put specific
# prefixes above general ones (`start_guide` before `start_`).
# ---------------------------------------------------------------------------

DEVICES = {
    "mount": {
        "events": {"AutoGoto", "AutoGotoStep", "ScopeHome", "OpenMount", "ScopeMove",
                   "ScopeTrack", "ScopePark", "ScopeSync", "MountStatus", "Slewing"},
        "methods": ("scope_", "start_auto_goto", "stop_goto", "set_sequence_mount"),
    },
    "guide": {
        "events": {"GuideStep", "GuideStar", "SettleDone", "Settling", "Calibrating",
                   "Calibration", "GuideCalibration", "StartGuide", "Guide",
                   "GuideLost", "Dither", "LoopingExposures"},
        "methods": ("guide", "start_guide", "stop_guide", "start_calibration",
                    "set_setting", "start_dither", "get_guide"),
    },
    "focuser": {
        "events": {"AutoFocus", "FocuserMove", "AutoFocusStep", "EAFMove"},
        "methods": ("move_focuser", "open_focuser", "close_focuser", "start_auto_focus",
                    "stop_auto_focus", "get_focuser"),
    },
    "solver": {
        "events": {"PlateSolve", "Annotate", "AutoCenter", "AutoCenterStep"},
        "methods": ("start_solve", "stop_solve", "get_last_solve_result",
                    "start_annotate", "get_annotate"),
    },
    "camera": {
        "events": {"Exposure", "ContinuousExposure", "Sequence", "AviRecord",
                   "VideoCapture", "AutoRun", "Autorun", "Stack", "PlanetStack",
                   "SaveImage", "NewImage"},
        "methods": ("start_exposure", "stop_exposure", "open_camera", "close_camera",
                    "set_control_value", "get_control_value", "start_record_avi",
                    "stop_record_avi", "set_subframe", "start_planet_stack",
                    "get_current_img", "set_sequence", "set_page"),
    },
    "system": {
        "events": {"PiStatus", "Shutdown", "Reboot", "WifiStatus", "BatteryStatus",
                   "DiskFull", "TempWarning"},
        "methods": ("pi_", "test_connection", "get_device_state", "get_power_supply",
                    "get_verify_str", "verify_client", "noop", "get_app_setting",
                    "get_setting"),
    },
}

# Events that are pure telemetry: correct, frequent, and not worth a row of the
# reader's attention unless something else has gone wrong. The dashboard folds
# these into a counter by default and expands them on demand.
NOISY_EVENTS = {"GuideStep", "PiStatus", "LoopingExposures", "ContinuousExposure"}


def classify(name=None, event=None, port=None):
    """Which subsystem a command or event belongs to."""
    if event:
        for dev, spec in DEVICES.items():
            if event in spec["events"]:
                return dev
        # Unknown events still carry a strong hint in their name.
        low = event.lower()
        for dev in ("guide", "mount", "focus", "solve", "expos", "camera"):
            if dev in low:
                return {"focus": "focuser", "solve": "solver",
                        "expos": "camera"}.get(dev, dev)
    if name:
        for dev, spec in DEVICES.items():
            for pre in spec["methods"]:
                if name == pre or name.startswith(pre):
                    return dev
    return PORT_HINT.get(port, "other")


def _clip(v):
    """Shrink a value to something a log line can carry."""
    if v is None:
        return None
    try:
        s = json.dumps(v, ensure_ascii=False, default=str)
    except Exception:
        s = str(v)
    if len(s) <= MAX_VALUE_CHARS:
        try:
            return json.loads(s)
        except Exception:
            return s
    return {"_truncated": len(s), "_head": s[:MAX_VALUE_CHARS]}


class Recorder:
    """Append-only JSONL sink. Safe to call from any thread, never raises.

    Never raises is load-bearing: this sits inside the socket reader thread and
    inside every call path in the toolkit. A dashboard that cannot write its log
    must not be able to take down a slew, so every public method swallows its
    own errors and disables itself after the first failure rather than raising
    once per guide step for the rest of the night.
    """

    def __init__(self, path=None, enabled=None):
        if enabled is None:
            enabled = os.environ.get("ASICAP_DASHBOARD", "1") not in ("0", "false", "no")
        self.enabled = enabled
        self._lock = threading.Lock()
        self._fh = None
        self._t0 = time.time()
        self.counts = {}
        self.path = path
        self._broken = None

    def _open(self):
        if self._fh is not None or not self.enabled:
            return self._fh
        try:
            if self.path is None:
                d = os.environ.get("ASICAP_DASHBOARD_DIR", DEFAULT_DIR)
                os.makedirs(d, exist_ok=True)
                stamp = datetime.datetime.now().strftime("%Y-%m-%d")
                self.path = os.path.join(d, "events-%s.jsonl" % stamp)
            self._fh = open(self.path, "a", buffering=1, encoding="utf-8")
            # A marker per process, so the dashboard can tell one tool's run
            # from the next within a single night's file.
            self._raw({"kind": "session", "pid": os.getpid(),
                       "argv": " ".join(os.sys.argv)[:300], "device": "system"})
        except Exception as e:
            self.enabled, self._broken = False, str(e)
        return self._fh

    def _raw(self, rec):
        rec.setdefault("t", datetime.datetime.now().isoformat(timespec="milliseconds"))
        rec.setdefault("dt0", round(time.time() - self._t0, 3))
        self._fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def write(self, **rec):
        if not self.enabled:
            return
        try:
            with self._lock:
                if self._open() is None:
                    return
                self._raw(rec)
                k = "%s/%s" % (rec.get("device", "?"), rec.get("kind", "?"))
                self.counts[k] = self.counts.get(k, 0) + 1
        except Exception as e:                  # a broken log must not break a slew
            self.enabled, self._broken = False, str(e)

    # -- the three taps -----------------------------------------------------

    def cmd(self, port, rid, method, params):
        self.write(kind="cmd", port=port, id=rid, method=method,
                   params=_clip(params), device=classify(name=method, port=port))

    def reply(self, port, rid, method, rep, dt):
        ok = not (isinstance(rep, dict) and rep.get("error"))
        r = {"kind": "reply", "port": port, "id": rid, "method": method,
             "dt": round(dt, 3), "ok": ok,
             "device": classify(name=method, port=port)}
        if ok:
            r["result"] = _clip(rep.get("result") if isinstance(rep, dict) else rep)
        else:
            r["error"] = rep.get("error")
            r["code"] = rep.get("code")
        self.write(**r)

    def event(self, port, msg):
        if not isinstance(msg, dict):
            self.write(kind="event", port=port, event="_raw", device="other",
                       data=_clip(msg))
            return
        name = msg.get("Event", "?")
        data = {k: v for k, v in msg.items() if k not in ("Event", "Timestamp")}
        self.write(kind="event", port=port, event=name,
                   device=classify(event=name, port=port),
                   noisy=name in NOISY_EVENTS,
                   air_time=msg.get("Timestamp"), data=_clip(data))

    # -- things the tools volunteer ----------------------------------------

    def note(self, text, device="app", level="info", **meta):
        """A human-readable marker on the timeline: what a script is about to do."""
        self.write(kind="note", text=str(text)[:500], device=device,
                   level=level, meta=_clip(meta) if meta else None)

    def artifact(self, kind, path, device=None, **meta):
        """An image or run directory the dashboard should display.

        `kind` is one of preview / focus_run / solve / video / other -- the
        dashboard keys its panes off it and shows the most recent of each.
        """
        self.write(kind="artifact", artifact=kind, path=os.path.abspath(path),
                   exists=os.path.exists(path),
                   device=device or {"preview": "camera", "focus_run": "focuser",
                                     "solve": "solver"}.get(kind, "other"),
                   meta=_clip(meta) if meta else None)

    def close(self):
        with self._lock:
            if self._fh:
                try:
                    self._fh.close()
                finally:
                    self._fh = None


rec = Recorder()


if __name__ == "__main__":
    # Self-check: prove the classifier does what the docstring claims.
    cases = [("scope_goto", None, 4400, "mount"),
             ("start_exposure", None, 4700, "camera"),
             ("move_focuser", None, 4700, "focuser"),
             ("start_solve", None, 4700, "solver"),
             ("pi_output_get2", None, 4700, "system"),
             ("start_guide", None, 4400, "guide"),
             (None, "GuideStep", 4400, "guide"),
             (None, "AutoGotoStep", 4400, "mount"),
             (None, "PlateSolve", 4700, "solver"),
             (None, "Exposure", 4700, "camera"),
             (None, "PiStatus", 4700, "system"),
             (None, "SomeNewGuiderThing", 4400, "guide"),
             (None, "TotallyUnknown", 4700, "camera")]
    bad = 0
    for name, ev, port, want in cases:
        got = classify(name=name, event=ev, port=port)
        flag = "ok " if got == want else "FAIL"
        bad += got != want
        print(f"  {flag} {str(name or ev):24} port {port} -> {got:8} (want {want})")
    print(("all %d ok" % len(cases)) if not bad else ("%d FAILED" % bad))
