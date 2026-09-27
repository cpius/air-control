#!/usr/bin/env python3
"""Run satvideo.py end to end against a FAKE Air that serves frames from the
pulled 2026-09-16 clips -- no sockets, no mount, nothing leaves this Mac.

Each scenario replays a way a night can go wrong and checks satvideo's exit
status, whether it started/stopped the recorder, and what it printed.

    python3 -u tests/test_satvideo_replay.py                  # all scenarios
    python3 -u tests/test_satvideo_replay.py --only clip1-0917 good

The 4800 stand-in behaves like the real socket where it matters: it serves
whatever image it holds (the cached one until a capture delivers), repeats an
image until the next exposure is due, and switches source when the recorder
starts, so "planet lost after N seconds" and "frames stall" can be staged.
"""
import argparse, io, os, runpy, signal, sys, tempfile, threading, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PLANETARY = os.path.join(HERE, "..", "planetary")
sys.path[:0] = [os.path.join(HERE, "..", "lib"), PLANETARY]
from clipcheck import Clip
import daypipes, main_image, mount

CLIPS = os.path.join(HERE, "..", "..", "Saturn", "2026-09-16")
SATURN = os.path.join(CLIPS, "short", "2026-09-17-002754-Alpheratz-Bin1 -10.1C.avi")   # Saturn in every frame, peak ~160
LAST_GOOD = os.path.join(CLIPS, "short", "2026-09-17-003019-Alpheratz-Bin1 -10.3C.avi")  # the run that crashed at 00:30:52
EMPTY = os.path.join(CLIPS, "2026-09-17-003222-Alpheratz-Bin1 -10.0C.avi")               # the first empty 300 s clip


class Source:
    """Frames of a clip, one new frame every 1/fps s from `start`."""

    def __init__(self, path, fps=4.0, first=0):
        self.r = Clip(path); self.count = len(self.r.offsets()); self.fps = fps; self.first = first; self.cache = (None, None)

    def frame(self, i):
        i = min(self.first + i, self.count - 1)
        if self.cache[0] != i:
            self.cache = (i, np.ascontiguousarray(self.r.frame(i)).tobytes())
        return self.cache[1]


class Air:
    """What get_current_img returns, as a function of what satvideo has asked for."""

    def __init__(self, name, cached, live, after_record=None, freeze_after=None, foreign_for=0.0, prime_miss=False):
        self.name = name
        self.cached = cached                # bytes of the image held before the capture
        self.live, self.after_record = live, after_record
        self.freeze_after = freeze_after    # seconds after start_record_avi when frames stop changing
        self.foreign_for = foreign_for      # seconds after start_exposure serving a 1920x1080 16-bit image
        self.prime_miss = prime_miss        # the cached frame only shows up AFTER start_exposure
        self.t_expo = self.t_rec = None
        self.calls = []
        self.gotos = []
        self.frozen = None
        self.last = None

    def c(self, method, params=None):
        self.calls.append(method)
        now = time.time()
        if method == "start_exposure":
            self.t_expo = now
        elif method == "start_record_avi":
            self.t_rec = now
        elif method == "get_subframe":
            return {"width": 1000, "height": 1000, "x": 1420, "y": 580}
        elif method == "get_camera_bin":
            return 1
        return 0

    def image(self):
        raw, w, h = self._image()
        self.last = (raw, w, h)
        return raw, w, h

    def _image(self):
        now = time.time()
        if self.t_expo is None:
            if self.prime_miss:
                return b"\x00\x10" * (1920 * 1080), 1920, 1080
            return self.cached, 1000, 1000
        dt = now - self.t_expo
        if self.prime_miss and dt < 1.0:
            return self.cached, 1000, 1000
        if dt < self.foreign_for:
            return b"\x00\x10" * (1920 * 1080), 1920, 1080
        if self.t_rec is not None and self.freeze_after is not None and now - self.t_rec > self.freeze_after:
            if self.frozen is None:
                self.frozen = self.last          # the image already served: nothing new from here on
            return self.frozen
        if self.t_rec is not None and self.after_record is not None and now - self.t_rec > self.after_record[0]:
            src = self.after_record[1]
            return src.frame(int((now - self.t_rec - self.after_record[0]) * src.fps)), 1000, 1000
        return self.live.frame(int(dt * self.live.fps)), 1000, 1000


AIR = None


class FakeImageSocket:
    def __init__(self, host=None):
        pass

    def get_image(self, method="get_current_img", cmd_id=0, wait=120):
        time.sleep(0.05)                                  # a download is not free
        raw, w, h = AIR.image()
        return {"width": w, "height": h, "isBigEndian": 0}, {"frame": raw}

    def close(self):
        pass


class FakeRpc:
    def drain_events(self):
        return []


class FakeSession:
    def __init__(self):
        self.air = FakeRpc()
        self.img = FakeImageSocket()


class FakePipes:
    def __init__(self, host=None, key=None):
        self.s = FakeSession(); self.exp = None; self.last_sig = None

    def c(self, m, p=None, t=25):
        return AIR.c(m, p)

    def setup(self, page, exp, gain, binning):
        AIR.calls.append("setup:" + page)

    def close(self):
        AIR.calls.append("close")


class FakeMount:
    def __init__(self, host=None, *a, **kw):
        self.ra, self.dec = 0.7167, 1.99

    def state(self):
        return {"RA": self.ra, "Dec": self.dec, "Alt": 32.0, "Az": 150.0, "pier_side": "west", "is_enable_track": True}

    def set_tracking(self, on):
        return 0

    def goto(self, ra, dec, wait=True, timeout=20):
        time.sleep(0.3)
        AIR.gotos.append((ra, dec)); self.ra, self.dec = ra, dec

    def close(self):
        pass


daypipes.Pipes = FakePipes
daypipes.host = lambda: "fake-air"
main_image.MainImage = FakeImageSocket
mount.Mount = FakeMount


class Tee(io.TextIOBase):
    def __init__(self):
        self.buf = io.StringIO()

    def write(self, s):
        sys.__stdout__.write(s); self.buf.write(s); return len(s)

    def flush(self):
        sys.__stdout__.flush()


def run(air, extra, sigterm_after_record=None):
    global AIR
    AIR = air
    argv = ["satvideo.py", "--seconds", "12", "--roi", "1000", "--exp-ms", "40", "--gain", "350",
            "--jacobian", "-46.0,599.6,-475.0,55.0", "--deadband", "100", "--min-gap", "4",
            "--max-missing", "10", "--max-stall", "8", "--heartbeat", "3", "--outdir", OUT] + extra
    tee = Tee()
    killer = None
    if sigterm_after_record is not None:
        def watch():
            while air.t_rec is None:
                time.sleep(0.1)
            time.sleep(sigterm_after_record)
            os.kill(os.getpid(), signal.SIGTERM)
        killer = threading.Thread(target=watch, daemon=True); killer.start()
    old_argv, old_out = sys.argv, sys.stdout
    sys.argv, sys.stdout = argv, tee
    code = None
    try:
        runpy.run_path(os.path.join(PLANETARY, "satvideo.py"), run_name="__main__")
        code = 0
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    except Exception as e:
        print("TRACEBACK %s: %s" % (e.__class__.__name__, e))
        code = "exception"
    finally:
        sys.argv, sys.stdout = old_argv, old_out
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    return code, tee.buf.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="scenario names to run")
    ap.add_argument("--outdir", default=os.path.join(tempfile.gettempdir(), "satvideo_replay"), help="test PNGs and hold CSVs go here, not telemetry/")
    args = ap.parse_args()
    global OUT
    OUT = args.outdir
    os.makedirs(OUT, exist_ok=True)
    for pth in (SATURN, LAST_GOOD, EMPTY):
        if not os.path.exists(pth):
            sys.exit("missing clip %s" % pth)
    last_good = Clip(LAST_GOOD); stale_saturn = last_good.frame(len(last_good.offsets()) - 1).tobytes()
    empty_cached = Clip(EMPTY).frame(0).tobytes()

    scenarios = [
        # name, Air, expected exit, must record?, text that must appear, text that must NOT appear, kwargs
        ("clip1-0917", lambda: Air("clip1-0917", stale_saturn, Source(EMPTY, first=100)),
         3, False, ["before setup: the Air holds", "ABORT"], ["RECORDED"], {}),
        ("clip1-0917-stale-after-start", lambda: Air("stale-after-start", stale_saturn, Source(EMPTY, first=100), prime_miss=True),
         3, False, ["ABORT"], ["RECORDED"], {}),
        ("good", lambda: Air("good", empty_cached, Source(SATURN)),
         0, True, ["confirm 2/2", "start_record_avi", "RECORDED", "RESULT recorded"], ["STOPPED EARLY"], {}),
        ("foreign-frame-first", lambda: Air("foreign", empty_cached, Source(SATURN), foreign_for=1.0),
         0, True, ["not the 1000x1000 ROI", "RECORDED"], [], {}),
        ("lost-mid-clip", lambda: Air("lost", empty_cached, Source(SATURN), after_record=(4.0, Source(EMPTY, first=500))),
         5, True, ["STOPPING: no planet in 10 consecutive fresh frames", "STOPPED EARLY", "RESULT lost"], ["RECORDED "], {}),
        ("frames-stall", lambda: Air("stall", empty_cached, Source(SATURN), freeze_after=3.0),
         6, True, ["STOPPING: no fresh frame", "RESULT stalled"], ["RECORDED "], {}),
        ("no-fresh-frame-while-recording", lambda: Air("nothing-new", empty_cached, Source(SATURN), freeze_after=0.0),
         6, True, ["frames stalled", "RESULT stalled"], ["RECORDED ", "planet lost", "STOPPING"], {"extra": ["--seconds", "3"]}),   # clip ends before --max-stall can
        ("sigterm-while-recording", lambda: Air("sigterm", empty_cached, Source(SATURN)),
         143, True, ["stop_record_avi (cleanup)"], ["RECORDED "], {"sigterm_after_record": 3.0}),
    ]
    results = []
    for name, make, want_code, must_record, must, must_not, kw in scenarios:
        if args.only and name not in args.only:
            continue
        print("\n" + "=" * 100 + "\nSCENARIO %s\n" % name + "=" * 100, flush=True)
        air = make()
        t = time.time()
        kw = dict(kw)
        code, out = run(air, kw.pop("extra", []), **kw)
        started = "start_record_avi" in air.calls
        stopped = air.calls.count("stop_record_avi")
        problems = []
        if code != want_code:
            problems.append("exit %s, wanted %s" % (code, want_code))
        if started != must_record:
            problems.append("start_record_avi %s" % ("called" if started else "never called"))
        if started and stopped == 0:
            problems.append("recording never stopped")
        problems += ["missing %r" % s for s in must if s not in out]
        problems += ["unexpected %r" % s for s in must_not if s in out]
        results.append((name, not problems, "exit %s, recorder %s, %d goto(s), %.0f s" % (
            code, "started+stopped" if started and stopped else ("started" if started else "not started"), len(air.gotos), time.time() - t), problems))
    print("\n" + "=" * 100, flush=True)
    for name, ok, info, problems in results:
        print("%s  %-30s %s%s" % ("PASS" if ok else "FAIL", name, info, "" if ok else "  <- " + "; ".join(problems)), flush=True)
    sys.exit(0 if all(r[1] for r in results) else 1)


if __name__ == "__main__":
    main()
