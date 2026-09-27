#!/usr/bin/env python3
"""Second-by-second logging for air-control. One line a second while anything is slow.

Everything in this toolkit talks to hardware over the network, and hardware is
slow in ways that are invisible from the outside: a focuser move is 2.1 ms per
step, a goto is tens of seconds, a 4800 download is megabytes over Wi-Fi, and an
Air that has quietly gone away looks exactly like an Air that is thinking. The
whole point of this module is that **a long silence is never acceptable output**.
If an operation can take several seconds, it says so every second while it runs.

Two things are provided.

`Log` — a plain levelled logger writing to stderr (so stdout stays pipeable),
optionally tee'd to a file with --log-file / AIRLOG_FILE. Levels are
error/warn/info/debug/trace; the default is info, `-v` gives debug, `-vv` trace.

`Ticker` — the interesting one. A context manager that starts a background
thread and emits a progress line every second until the block exits:

    with log.slow("goto", detail=lambda: f"RA={ra:.4f}") as tk:
        ...                       # 14:22:03  +12.0s  info  mount: ... goto 12.0s RA=20.0161

It is a THREAD, deliberately, not a poll inside the caller's loop. Most of the
slow paths here block in `socket.recv` or `time.sleep`, where no amount of
caller-side bookkeeping can produce a heartbeat. The thread ticks regardless of
what the main thread is doing — including inside a tight pure-Python pixel loop,
since the interpreter switches threads every few milliseconds.

`quiet_for` keeps it honest: nothing is printed until the operation has actually
been slow (1 s by default), so the fast path stays silent and every tick line in
the log means something really did take a second. The closing line is only
printed if the operation ticked at least once.

`detail` is a callable evaluated at tick time, so the line carries live values
(position, bytes received, frame count) rather than just a stopwatch. It is
called from the ticker thread — keep it cheap and non-blocking. Exceptions in it
are swallowed; a broken detail callback must never take down a slew.

    from airlog import get_logger, add_log_args, configure_logging
    log = get_logger("focus")
    log.info("sweep %d..%d", lo, hi)
    with log.slow("focuser move", detail=lambda: f"at {pos}"):
        ...
"""

import os
import sys
import threading
import time

ERROR, WARN, INFO, DEBUG, TRACE = 40, 30, 20, 10, 5
_NAMES = {"error": ERROR, "warn": WARN, "warning": WARN,
          "info": INFO, "debug": DEBUG, "trace": TRACE}
_LABEL = {ERROR: "ERROR", WARN: "warn", INFO: "info", DEBUG: "debug", TRACE: "trace"}

_T0 = time.time()
_lock = threading.Lock()
_level = _NAMES.get(os.environ.get("AIRLOG", "info").strip().lower(), INFO)
_fh = None                      # optional tee-to-file handle
_stream = sys.stderr


def set_level(level):
    """`level` is a name ('debug') or one of the module constants."""
    global _level
    _level = _NAMES.get(str(level).lower(), level) if not isinstance(level, int) else level


def get_level():
    return _level


def enabled(level):
    return level >= _level


def set_file(path):
    """Tee every line to `path` as well as stderr. Appends; line-buffered."""
    global _fh
    if _fh is not None:
        try:
            _fh.close()
        except Exception:
            pass
        _fh = None
    if path:
        _fh = open(path, "a", buffering=1)
        _fh.write("\n===== %s  pid %d  %s =====\n"
                  % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(),
                     " ".join(sys.argv)))


def _write(line):
    with _lock:
        try:
            _stream.write(line + "\n")
            _stream.flush()
        except Exception:
            pass
        if _fh is not None:
            try:
                _fh.write(line + "\n")
            except Exception:
                pass


def _emit(level, scope, msg):
    _write("%s %+8.1fs %-5s %-11s %s"
           % (time.strftime("%H:%M:%S"), time.time() - _T0, _LABEL.get(level, "?"),
              scope + ":", msg))


class Ticker:
    """Emit `label` every `interval` seconds until the block exits.

    Silent for the first `quiet_for` seconds so the fast path stays quiet; the
    closing "took Xs" line only appears if it ticked at all. `detail` is called
    on the ticker thread each tick and may return None.
    """

    def __init__(self, log, label, interval=1.0, quiet_for=1.0, detail=None,
                 level=INFO):
        self.log = log
        self.label = label
        self.interval = max(0.05, float(interval))
        self.quiet_for = float(quiet_for)
        self.detail = detail
        self.level = level
        self.t0 = None
        self.ticks = 0
        self.note = None            # sticky text, set from the caller's loop
        self._stop = threading.Event()
        self._th = None

    # -- caller-side helpers -------------------------------------------------
    @property
    def elapsed(self):
        return time.time() - (self.t0 or time.time())

    def set(self, note):
        """Sticky detail shown on subsequent ticks (cheap: a bare assignment)."""
        self.note = note

    def event(self, msg, *args):
        """Log something that happened mid-operation, with the running clock."""
        if enabled(self.level):
            _emit(self.level, self.log.scope,
                  "%s %5.1fs | %s" % (self.label, self.elapsed,
                                      (msg % args) if args else msg))

    # -- context manager -----------------------------------------------------
    def __enter__(self):
        self.t0 = time.time()
        self.log.debug("%s ...", self.label)
        if enabled(self.level):
            self._th = threading.Thread(target=self._run, daemon=True)
            self._th.start()
        return self

    def _describe(self):
        bits = []
        if self.note:
            bits.append(str(self.note))
        if self.detail is not None:
            try:
                d = self.detail()
            except Exception as e:                   # never break the operation
                d = "<detail failed: %s>" % e
            if d:
                bits.append(str(d))
        return "  ".join(bits)

    def _run(self):
        # Wait out the quiet period, then tick on a fixed cadence.
        if self._stop.wait(self.quiet_for):
            return
        nxt = self.t0 + max(self.quiet_for, self.interval)
        while not self._stop.wait(max(0.0, nxt - time.time())):
            self.ticks += 1
            _emit(self.level, self.log.scope,
                  "... %s %5.1fs  %s" % (self.label, time.time() - self.t0,
                                         self._describe()))
            nxt += self.interval
            if nxt < time.time():                    # a long stall: don't burst
                nxt = time.time() + self.interval

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._th is not None:
            self._th.join(timeout=0.3)
        dt = time.time() - self.t0
        if exc_type is not None:
            self.log.warn("%s FAILED after %.1fs: %s: %s",
                          self.label, dt, exc_type.__name__, exc)
        elif self.ticks:
            _emit(self.level, self.log.scope,
                  "    %s done in %.1fs  %s" % (self.label, dt, self._describe()))
        else:
            self.log.debug("%s done in %.2fs", self.label, dt)
        return False


class Log:
    def __init__(self, scope):
        self.scope = scope

    def _log(self, level, msg, args):
        if enabled(level):
            _emit(level, self.scope, (msg % args) if args else str(msg))

    def error(self, msg, *a):  self._log(ERROR, msg, a)
    def warn(self, msg, *a):   self._log(WARN, msg, a)
    def info(self, msg, *a):   self._log(INFO, msg, a)
    def debug(self, msg, *a):  self._log(DEBUG, msg, a)
    def trace(self, msg, *a):  self._log(TRACE, msg, a)

    def slow(self, label, interval=1.0, quiet_for=1.0, detail=None, level=INFO):
        """Context manager that ticks once a second while the block runs."""
        return Ticker(self, label, interval=interval, quiet_for=quiet_for,
                      detail=detail, level=level)


_logs = {}


def get_logger(scope):
    return _logs.setdefault(scope, Log(scope))


def _add_flags(ap, default_suppress=False):
    import argparse
    d = argparse.SUPPRESS if default_suppress else None
    ap.add_argument("-v", "--verbose", action="count",
                    default=(d if default_suppress else 0),
                    help="-v logs every RPC call and frame; -vv adds wire detail")
    ap.add_argument("-q", "--quiet", action="store_true",
                    default=(d if default_suppress else False),
                    help="warnings and errors only (per-second ticks are silenced)")
    ap.add_argument("--log-file", metavar="PATH",
                    default=(d if default_suppress
                             else os.environ.get("AIRLOG_FILE")),
                    help="append the full log here as well as to stderr")


def add_log_args(ap):
    """Add -v/-q/--log-file to a parser. Call configure_logging(args) after.

    Also copies the flags onto every subparser already registered, so both
    `tool -v goto ...` and `tool goto ... -v` work. Argparse otherwise accepts
    only the first, which is not where anyone types it. The copies default to
    SUPPRESS so an unused subparser flag cannot clobber the value the top-level
    parser set -- the usual argparse trap.

    Call this AFTER the subparsers are defined.
    """
    import argparse
    _add_flags(ap)
    for action in ap._actions:
        if isinstance(action, argparse._SubParsersAction):
            for spar in action.choices.values():
                try:
                    _add_flags(spar, default_suppress=True)
                except argparse.ArgumentError:
                    pass                # the subcommand defines its own -v/-q
    return ap


def configure_logging(args):
    if getattr(args, "quiet", False):
        set_level(WARN)
    elif getattr(args, "verbose", 0) >= 2:
        set_level(TRACE)
    elif getattr(args, "verbose", 0) == 1:
        set_level(DEBUG)
    if getattr(args, "log_file", None):
        set_file(args.log_file)
    return args


if os.environ.get("AIRLOG_FILE"):
    try:
        set_file(os.environ["AIRLOG_FILE"])
    except OSError as e:
        _emit(WARN, "airlog", "cannot open AIRLOG_FILE: %s" % e)
