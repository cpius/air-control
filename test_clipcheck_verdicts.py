#!/usr/bin/env python3
"""Run clipcheck.py --air end to end against SYNTHETIC Air clips in a temp folder
(--video-dir, so nothing is mounted and nothing leaves this Mac).

Each scenario writes the files a clip leaves in Video/ -- an AVI with the Air's
RIFF/strf header and '00db' frame chunks, and a 400x400 _thn.jpg -- in some state
a night can produce, and checks clipcheck's exit status and what it printed.

    python3 -u test_clipcheck_verdicts.py                    # all scenarios
    python3 -u test_clipcheck_verdicts.py --only recording-no-frames good
    python3 -u test_clipcheck_verdicts.py --clipcheck /some/other/clipcheck.py

The regression: 2026-09-26 23:49, a manual check of a clip still being recorded
(0.20 GB) read no frame anywhere in it and still said VERDICT PLANET, from the
thumbnail -- the first frame -- alone. That must be UNCHECKED (exit 1), and
PLANET needs at least 2 of the 3 blocks readable.
"""
import argparse, os, shutil, struct, subprocess, sys, tempfile, threading, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
W = H = 300                       # ROI; a 120 px Saturn-sized disc clears the default detector's minimum area
SINCE = "2026-09-26-234855"
NAME = "2026-09-26-234915-Moon-Bin1 -10.3C.avi"
OLDER = "2026-09-26-234033-Moon-Bin1 -10.5C.avi"
RNG = np.random.default_rng(20260926)


def frame(planet):
    f = RNG.normal(12.0, 3.0, (H, W))
    if planet:
        yy, xx = np.mgrid[:H, :W]
        f[(yy - 140) ** 2 + (xx - 160) ** 2 <= 60 ** 2] += 140.0
    return np.clip(f, 0, 255).astype(np.uint8)


def avi(path, frames=(), zeros=0):
    """The Air's AVI as clipcheck sees it: RIFF + strf (BITMAPINFOHEADER) + movi,
    then one '00db' chunk per frame, then `zeros` bytes of nothing -- what a file
    still being written can show over SMB (its size known, its frames not yet)."""
    bih = struct.pack("<IiiHHIIiiII", 40, W, H, 1, 8, 0, W * H, 0, 0, 256, 0)
    hdrl = b"hdrl" + b"strf" + struct.pack("<I", len(bih)) + bih
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 0) + b"AVI " + b"LIST" + struct.pack("<I", len(hdrl)) + hdrl
                + b"LIST" + struct.pack("<I", 0) + b"movi")
        for p in frames:
            f.write(b"00db" + struct.pack("<I", W * H) + frame(p).tobytes())
        f.write(b"\0" * zeros)


def thumb(path, planet):
    import cv2
    g = frame(planet).astype(np.float32)
    g = cv2.resize(g, (400, 400), interpolation=cv2.INTER_LINEAR)
    cv2.imwrite(path, cv2.cvtColor(np.clip(g, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR))


def clip(folder, name, frames=(), zeros=0, thn=None):
    avi(os.path.join(folder, name), frames, zeros)
    if thn is not None:
        thumb(os.path.join(folder, name[:-4] + "_thn.jpg"), thn)


def grow(path, seconds=6.0, step=1 << 20):
    """Append zeros for `seconds`, like the Air's recorder, in the background."""
    def run():
        t0 = time.time()
        while time.time() - t0 < seconds:
            with open(path, "ab") as f:
                f.write(b"\0" * step)
            time.sleep(0.5)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


MB = 1 << 20

# name, build(folder) -> background thread or None, extra args, expected exit, text that must appear, text that must NOT appear
SCENARIOS = [
    ("recording-no-frames", lambda d: clip(d, NAME, zeros=8 * MB, thn=True),
     [], 1, ["no frames readable", "VERDICT UNCHECKED", "none of the 3 sampled blocks", "thumbnail (planet) is only the first frame"],
     ["VERDICT PLANET"]),
    ("header-only", lambda d: clip(d, NAME, thn=True),
     [], 1, ["VERDICT UNCHECKED"], ["VERDICT PLANET"]),
    ("no-frames-empty-thumbnail", lambda d: clip(d, NAME, zeros=8 * MB, thn=False),
     [], 1, ["VERDICT UNCHECKED"], ["VERDICT EMPTY", "VERDICT PLANET"]),
    ("still-growing", lambda d: (clip(d, NAME, zeros=2 * MB, thn=True), grow(os.path.join(d, NAME)))[1],
     ["--settle", "4"], 1, ["still growing after 4 s", "VERDICT UNCHECKED"], ["VERDICT PLANET"]),
    ("one-block-readable", lambda d: clip(d, NAME, [True] * 40, zeros=30 * MB, thn=True),
     [], 1, ["VERDICT UNCHECKED", "only 1 of 3 blocks readable (PLANET needs 2)"], ["VERDICT PLANET"]),
    ("two-blocks-readable", lambda d: clip(d, NAME, [True] * 120, zeros=7 * MB, thn=True),
     [], 0, ["VERDICT PLANET", "2 of 3 blocks readable"], []),
    ("one-block-with-min-blocks-1", lambda d: clip(d, NAME, [True] * 40, zeros=30 * MB, thn=True),
     ["--min-blocks", "1"], 0, ["VERDICT PLANET", "1 of 3 blocks readable"], []),
    ("good", lambda d: clip(d, NAME, [True] * 100, thn=True),
     [], 0, ["frames 300x300, from the AVI header", "VERDICT PLANET", "3 of 3 blocks readable"], ["no frames readable"]),
    ("empty", lambda d: clip(d, NAME, [False] * 100, thn=False),
     [], 2, ["VERDICT EMPTY"], ["VERDICT PLANET"]),
    ("lost-midway", lambda d: clip(d, NAME, [True] * 50 + [False] * 50, thn=True),
     [], 3, ["VERDICT PARTIAL"], ["VERDICT PLANET"]),
    # tonight's manual check: an older finished clip and the one still being recorded
    ("newest-is-recording", lambda d: (clip(d, OLDER, [True] * 100, thn=True), clip(d, NAME, zeros=8 * MB, thn=True)),
     ["--since", "2026-09-26-233400"], 1, ["clip " + NAME, "VERDICT UNCHECKED"], ["VERDICT PLANET"]),
    ("until-skips-recording", lambda d: (clip(d, OLDER, [True] * 100, thn=True), clip(d, NAME, zeros=8 * MB, thn=True)),
     ["--since", "2026-09-26-233400", "--until", "2026-09-26-234500"], 0, ["clip " + OLDER, "VERDICT PLANET"], ["234915"]),
    ("until-before-every-clip", lambda d: (clip(d, OLDER, [True] * 100, thn=True), clip(d, NAME, zeros=8 * MB, thn=True)),
     ["--since", "2026-09-26-233400", "--until", "2026-09-26-233900"], 2,
     ["no new clip", "and at or before 2026-09-26-233900", "after --until"], ["VERDICT PLANET"]),
]


def run(clipcheck, folder, extra):
    # --host/--mountpoint point nowhere real: --video-dir means no mount, and if that ever broke it could not reach the Air
    argv = [sys.executable, "-u", clipcheck, "--air", "--video-dir", folder, "--since", SINCE, "--settle", "0",
            "--host", "127.0.0.1", "--mountpoint", os.path.join(folder, "_no_mount")] + extra    # a later --since/--settle wins
    print("$ " + " ".join(argv), flush=True)
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    out = []
    for line in p.stdout:                  # streamed as it comes, and kept for the checks
        sys.stdout.write(line); sys.stdout.flush()
        out.append(line)
    return p.wait(), "".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="scenario names to run")
    ap.add_argument("--clipcheck", default=os.path.join(HERE, "clipcheck.py"), help="the clipcheck.py under test")
    ap.add_argument("--workdir", help="synthetic clips go here (default: a fresh temp folder, removed afterwards)")
    ap.add_argument("--keep", action="store_true", help="keep the synthetic clips")
    args = ap.parse_args()
    work = args.workdir or tempfile.mkdtemp(prefix="clipcheck_verdicts_")
    os.makedirs(work, exist_ok=True)
    results = []
    try:
        for name, build, extra, want_code, must, must_not in SCENARIOS:
            if args.only and name not in args.only:
                continue
            print("\n" + "=" * 100 + "\nSCENARIO %s\n" % name + "=" * 100, flush=True)
            folder = os.path.join(work, name)
            shutil.rmtree(folder, ignore_errors=True)
            os.makedirs(folder)
            t = time.time()
            bg = build(folder)
            code, out = run(args.clipcheck, folder, extra)
            if isinstance(bg, threading.Thread):
                bg.join()
            problems = []
            if code != want_code:
                problems.append("exit %s, wanted %s" % (code, want_code))
            problems += ["missing %r" % s for s in must if s not in out]
            problems += ["unexpected %r" % s for s in must_not if s in out]
            results.append((name, not problems, "exit %s, %.1f s" % (code, time.time() - t), problems))
    finally:
        if not args.keep and not args.workdir:
            shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 100, flush=True)
    for name, ok, info, problems in results:
        print("%s  %-30s %s%s" % ("PASS" if ok else "FAIL", name, info, "" if ok else "  <- " + "; ".join(problems)), flush=True)
    sys.exit(0 if results and all(r[1] for r in results) else 1)


if __name__ == "__main__":
    main()
