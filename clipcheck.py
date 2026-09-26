#!/usr/bin/env python3
"""Does a planetary clip actually contain the planet? Judged from the FILE, never
from the log of the loop that recorded it.

On 2026-09-17 six 300 s Saturn clips were reported recorded and were empty sky
(memory: check-frames-not-logs). This looks at what was written: the Air's
_thn.jpg (its first frame, 400x400) and 20-frame means taken at points through the
AVI, each run through planetdetect.detect().

Two modes.

On the Air, after a clip (what satloop.sh runs). Mounts the guest share
read-only, takes the newest AVI in Video/ named at or after --since (and at or
before --until), and reads it in place -- nothing is copied, so the root-only
'arch' flag on the Air's files never matters. Exit 0 planet, 2 empty or no new
clip, 3 partial, 1 could not check.

    python3 -u clipcheck.py --air --host 192.168.1.35 --since 2026-09-17-003217
    python3 -u clipcheck.py --air --since 2026-09-26-233400 --until 2026-09-26-234500   # not the clip being recorded

The thumbnail is the clip's FIRST frame, written when recording starts, so it says
nothing about what the file holds. PLANET needs the planet in at least --min-blocks
(2) readable block means; with no block readable the verdict is UNCHECKED whatever
the thumbnail shows. (2026-09-26 23:49: a clip still being recorded, no frame
readable anywhere in it, passed as PLANET on its thumbnail alone.)

Pulled files, every frame (validation). With --expect, PASS/FAIL per clip and
exit 0 only if every clip matches:

    python3 -u clipcheck.py --files "../Saturn/2026-09-16/short/*.avi" --expect planet
    python3 -u clipcheck.py --files "../Saturn/2026-09-16/2026-09-17-00[345]*.avi" --expect empty

AVIs are read chunk by chunk ('00db' chunks of width*height bytes, resyncing over
anything else), so a file whose header was zeroed by a power loss still reads,
and so does every OpenDML segment of a >1 GB capture.
"""
import argparse, glob, os, re, struct, subprocess, sys, tempfile, time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from planetdetect import detect, detector_args, detector_kw

T0 = time.time()


def log(msg):
    print("%s %+7.1fs  %s" % (time.strftime("%H:%M:%S"), time.time() - T0, msg), flush=True)


# -- reading clips ----------------------------------------------------------------

FOURCC_AFTER_FRAME = (b"00db", b"00dc", b"ix00", b"idx1", b"LIST", b"RIFF", b"JUNK")


class Clip:
    """Frames of an Air RAW8 AVI (header optional) or a SER file."""

    def __init__(self, path, roi=1000):
        self.path = path
        self.f = open(path, "rb")
        self.size = os.fstat(self.f.fileno()).st_size
        head = self.f.read(65536)
        self.ser = head[:14] == b"LUCAM-RECORDER"
        if self.ser:
            self.width, self.height, depth, self.count = struct.unpack("<iiii", head[26:42])
            if depth != 8:
                raise ValueError("%s: %d-bit SER, only 8-bit is handled" % (path, depth))
        else:
            k = head.find(b"strf") if head[:4] == b"RIFF" else -1
            if k >= 0:
                self.width, self.height = struct.unpack("<ii", head[k + 12:k + 20])
                self.height = abs(self.height)
                self.header = "from the AVI header"
            else:
                self.width = self.height = roi
                self.header = "header unreadable -> assumed the %dx%d ROI" % (roi, roi)
            self.count = None
        self.fb = self.width * self.height
        self._offsets = None
        self.on_scan = None     # called with the file offset as _resync reads each window (heartbeats)
        self.chunk = re.compile(rb"00d[bc]" + re.escape(struct.pack("<I", self.fb)))

    def close(self):
        self.f.close()

    def _is_frame_chunk(self, pos):
        if pos + 8 + self.fb > self.size:
            return False
        self.f.seek(pos)
        h = self.f.read(8)
        return h[:3] == b"00d" and h[3:4] in (b"b", b"c") and struct.unpack("<I", h[4:])[0] == self.fb

    def _resync(self, pos):
        """Offset of the next genuine frame chunk at or after pos, or None. A match is
        genuine if what follows the frame is another chunk (or the end of the file),
        so pixel bytes that happen to spell '00db' are not taken for a header."""
        win = 4 << 20
        while pos < self.size:
            if self.on_scan:
                self.on_scan(pos)
            self.f.seek(pos)
            buf = self.f.read(win + 8)
            for m in self.chunk.finditer(buf):
                cand = pos + m.start()
                nxt = cand + 8 + self.fb + (self.fb & 1)
                if nxt + 8 > self.size:
                    if cand + 8 + self.fb <= self.size:
                        return cand
                    continue
                self.f.seek(nxt)
                if self.f.read(4) in FOURCC_AFTER_FRAME:
                    return cand
            if len(buf) <= 8:
                return None
            pos += win
        return None

    def frames(self, start_frac=0.0, n=None, every=1):
        """Yield (index-in-this-walk, HxW uint8) from start_frac of the file on."""
        if self.ser:
            first = int(start_frac * self.count)
            last = self.count if n is None else min(self.count, first + n * every)
            for j, i in enumerate(range(first, last, every)):
                self.f.seek(178 + i * self.fb)
                yield j, np.frombuffer(self.f.read(self.fb), np.uint8).reshape(self.height, self.width)
            return
        pos = self._resync(int(start_frac * self.size))
        j = i = 0
        while pos is not None and (n is None or j < n):
            if not self._is_frame_chunk(pos):
                pos = self._resync(pos + 1)
                continue
            if i % every == 0:
                self.f.seek(pos + 8)
                yield j, np.frombuffer(self.f.read(self.fb), np.uint8).reshape(self.height, self.width)
                j += 1
            i += 1
            pos += 8 + self.fb + (self.fb & 1)

    def offsets(self):
        """File offset of every frame's pixels, found by walking the chunk headers once
        (no pixels are read)."""
        if self._offsets is None:
            if self.ser:
                self._offsets = [178 + i * self.fb for i in range(self.count)]
            else:
                offs, pos = [], self._resync(0)
                while pos is not None:
                    if not self._is_frame_chunk(pos):
                        pos = self._resync(pos + 1)
                        continue
                    offs.append(pos + 8)
                    pos += 8 + self.fb + (self.fb & 1)
                self._offsets = offs
        return self._offsets

    def frame(self, i):
        """Frame i as HxW uint8, by random access."""
        self.f.seek(self.offsets()[i])
        return np.frombuffer(self.f.read(self.fb), np.uint8).reshape(self.height, self.width)

    def block_mean(self, frac, n=20):
        acc, got = None, 0
        for _j, fr in self.frames(frac, n):
            acc = fr.astype(np.float32) if acc is None else acc + fr
            got += 1
        return (acc / got, got) if got else (None, 0)


def thumb_of(path):
    stem = re.sub(r"(_recovered)?\.(avi|ser)$", "", path, flags=re.I)
    t = stem + "_thn.jpg"
    return t if os.path.exists(t) else None


def check_thumb(path, a, roi_w):
    import cv2
    im = cv2.imread(path)
    if im is None:
        return None
    g = im.astype(np.float32).mean(axis=2)
    return detect(g, **detector_kw(a, scale=g.shape[1] / float(roi_w)))


# -- mode 1: pulled files ------------------------------------------------------------

def check_file(path, a):
    name = os.path.basename(path)
    short = name[11:17] if re.match(r"\d{4}-\d{2}-\d{2}-\d{6}", name) else name[:20]
    c = Clip(path, roi=a.roi)
    kw = detector_kw(a)
    r = dict(path=path, short=short, frames=0, planet=0, snr=[], area=[], rej_snr=0.0, rej_area=0, rej_amp=0.0,
             blocks=[], thumb=None, first_miss=None)
    log("%s  %dx%d  %s  %.2f GB%s" % (short, c.width, c.height, "SER" if c.ser else "AVI", c.size / 1e9,
                                        "" if c.ser else "  (" + c.header + ")"))
    last = time.time()
    for j, fr in c.frames(every=a.every):
        d = detect(fr, **kw)
        r["frames"] += 1
        if d.ok:
            r["planet"] += 1
            r["snr"].append(d.snr)
            r["area"].append(d.area)
        else:
            r["rej_snr"] = max(r["rej_snr"], d.snr)
            r["rej_area"] = max(r["rej_area"], d.area)
            r["rej_amp"] = max(r["rej_amp"], d.amp)
            if r["first_miss"] is None:
                r["first_miss"] = (j * a.every, str(d))
        if time.time() - last >= a.heartbeat:
            last = time.time()
            log("%s  ... %d frames, planet in %d" % (short, r["frames"], r["planet"]))
    for frac in a.block_at:
        m, got = c.block_mean(frac, a.block)
        d = detect(m, **kw) if m is not None else None
        r["blocks"].append((frac, got, d))
    t = thumb_of(path)
    if t:
        r["thumb"] = check_thumb(t, a, c.width)
    c.close()
    return r


def summarise(r, expect):
    n, k = r["frames"], r["planet"]
    blocks_ok = sum(1 for _f, _g, d in r["blocks"] if d is not None and d.ok)
    unreadable = sum(1 for _f, got, _d in r["blocks"] if not got)
    parts = ["%s  planet in %d/%d frames (%.1f%%)" % (r["short"], k, n, 100.0 * k / max(n, 1))]
    if k:
        parts.append("SNR min %.0f median %.0f, area min %d median %d px" % (
            min(r["snr"]), float(np.median(r["snr"])), min(r["area"]), int(np.median(r["area"]))))
    if k < n:
        parts.append("rejected frames peak at SNR %.1f, %.1f counts, %d px" % (r["rej_snr"], r["rej_amp"], r["rej_area"]))
    parts.append("%d-frame means %d/%d%s" % (a.block, blocks_ok, len(r["blocks"]), " (%d unreadable)" % unreadable if unreadable else ""))
    th = r["thumb"]
    parts.append("thumbnail %s" % ("-" if th is None else ("planet" if th.ok else "empty")))
    ok = None
    if expect == "planet":
        ok = (k >= a.min_planet_frac * n and n > 0 and blocks_ok == len(r["blocks"]) and (th is None or th.ok))
    elif expect == "empty":
        ok = (k == 0 and n > 0 and blocks_ok == 0 and (th is None or not th.ok))
    line = " | ".join(parts)
    if ok is not None:
        line = ("PASS  " if ok else "FAIL  ") + line
    return ok, line


# -- mode 2: the newest clip on the Air -----------------------------------------------

def mounted(mp):
    out = subprocess.run(["mount"], capture_output=True, text=True).stdout
    return any(" on %s (" % mp in line for line in out.splitlines())


def ensure_mount(a, force=False):
    """Same share, options and default mountpoint as tools/airpull.py."""
    mp = os.path.realpath(a.mountpoint)
    if force and mounted(mp):
        subprocess.run(["umount", "-f", mp], capture_output=True)
    if mounted(mp):
        return mp
    os.makedirs(mp, exist_ok=True)
    url = "//guest:@%s/%s" % (a.host, a.share.replace(" ", "%20"))
    r = subprocess.run(["mount_smbfs", "-o", "ro,soft", url, mp], capture_output=True, text=True)
    if r.returncode != 0 or not mounted(mp):
        raise OSError("mount_smbfs %s failed: %s" % (url, r.stderr.strip() or r.returncode))
    log("mounted %s read-only at %s" % (url, mp))
    return mp


NAME_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{6})-.*\.avi$", re.I)


def stamp(s):
    """argparse type for --since/--until: names are compared as text, so a malformed
    bound would silently match nothing (and read as 'no new clip')."""
    time.strptime(s, "%Y-%m-%d-%H%M%S")
    return s


def newest_clip(folder, since, skew, exclude=(), until=None):
    """(newest AVI named at or after since-skew, at or before until (as named, no
    skew) and not in exclude; newest AVI of all)."""
    lo = time.strftime("%Y-%m-%d-%H%M%S", time.localtime(time.mktime(time.strptime(since, "%Y-%m-%d-%H%M%S")) - skew))
    avis = sorted((n for n in os.listdir(folder) if NAME_TS.match(n)), key=lambda n: NAME_TS.match(n).group(1))
    names = [n for n in avis if NAME_TS.match(n).group(1) >= lo and (until is None or NAME_TS.match(n).group(1) <= until)
             and n not in exclude]
    return (names[-1] if names else None), (avis[-1] if avis else None)


def air_verdict(name, thumb, blocks, sampled, min_blocks):
    """(exit code, VERDICT line). thumb: the thumbnail's detection verdict, None if
    there was none; blocks: the verdict of each block mean that could be read, out
    of `sampled`. The thumbnail alone never makes a verdict (module docstring)."""
    n = len(blocks)
    read = "%d of %d blocks readable" % (n, sampled)
    th = "no thumbnail" if thumb is None else "thumbnail %s" % ("planet" if thumb else "empty")
    if n == 0:
        return 1, "VERDICT UNCHECKED: %s -- none of the %d sampled blocks could be read%s" % (
            name, sampled, "" if thumb is None else "; the thumbnail (%s) is only the first frame" % ("planet" if thumb else "empty"))
    evidence = blocks + ([] if thumb is None else [thumb])
    if not any(evidence):
        return 2, "VERDICT EMPTY: %s -- no planet in the thumbnail or any readable block (%s)" % (name, read)
    if all(evidence):
        if n < min_blocks:
            return 1, "VERDICT UNCHECKED: %s -- planet wherever it could be read, but only %s (PLANET needs %d)" % (name, read, min_blocks)
        return 0, "VERDICT PLANET: %s -- planet in %severy readable block (%s)" % (name, "the thumbnail and " if thumb else "", read)
    return 3, "VERDICT PARTIAL: %s -- planet in %d of %d samples (%s, %s)" % (name, sum(evidence), len(evidence), th, read)


def check_air(a):
    for attempt in range(a.retries + 1):
        try:
            folder = a.video_dir or os.path.join(ensure_mount(a, force=attempt > 0), a.folder)
            name, newest = newest_clip(folder, a.since, a.skew, a.exclude or (), a.until)
            break
        except OSError as e:
            log("share not readable (%s: %s)%s" % (e.__class__.__name__, e, "; retry in 10 s" if attempt < a.retries else ""))
            if attempt < a.retries:
                time.sleep(10)
    else:
        log("VERDICT UNCHECKED: could not read %s" % (a.video_dir or "//%s/%s/%s" % (a.host, a.share, a.folder)))
        return 1
    if name is None:
        log("VERDICT EMPTY: no new clip in %s named at or after %s (-%d s skew)%s%s -- nothing was recorded" % (
            a.folder, a.since, a.skew, "" if a.until is None else " and at or before %s" % a.until,
            "" if newest is None else "; the newest there is %s, %s" % (
                newest, "already checked" if newest in (a.exclude or ()) else "after --until" if a.until and NAME_TS.match(newest).group(1) > a.until
                else "and if that is the new clip the Air's clock is behind this Mac's")))
        return 2
    path = os.path.join(folder, name)
    size, t0, growing = os.path.getsize(path), time.time(), False
    while time.time() - t0 < a.settle:
        time.sleep(2.0)
        now = os.path.getsize(path)
        growing = now != size
        if not growing:
            break
        log("  %s still growing (%.2f GB), waiting" % (name, now / 1e9))
        size = now
    log("clip %s  %.2f GB" % (name, size / 1e9))
    if growing:
        log("  still growing after %.0f s: probably still being recorded -- only what is written so far is judged" % a.settle)
    thumb, blocks = None, []
    t = os.path.join(folder, re.sub(r"\.avi$", "", name, flags=re.I) + "_thn.jpg")
    try:
        c = Clip(path, roi=a.roi)
        log("  frames %dx%d, %s" % (c.width, c.height, c.header))
        last = [time.time()]

        def scanning(pos):
            if time.time() - last[0] >= a.heartbeat:
                last[0] = time.time()
                log("    ... looking for a %dx%d frame at %.2f of %.2f GB" % (c.width, c.height, pos / 1e9, c.size / 1e9))
        c.on_scan = scanning
        if os.path.exists(t):
            d = check_thumb(t, a, c.width)
            if d is not None:
                thumb = d.ok
                log("  thumbnail (first frame): %s" % d)
        else:
            log("  no thumbnail %s" % os.path.basename(t))
        kw = detector_kw(a)
        for frac in a.block_at:
            t1 = last[0] = time.time()
            m, got = c.block_mean(frac, a.block)
            if m is None:
                log("  %3.0f%% into the file: no frames readable (no %dx%d frame chunk from there to the end, %.1f s)" % (
                    100 * frac, c.width, c.height, time.time() - t1))
                continue
            d = detect(m, **kw)
            blocks.append(d.ok)
            log("  %3.0f%% into the file: mean of %d frames: %s  (read in %.1f s)" % (100 * frac, got, d, time.time() - t1))
        c.close()
    except OSError as e:
        log("VERDICT UNCHECKED: reading %s failed after %d sample(s): %s: %s" % (
            name, len(blocks) + (thumb is not None), e.__class__.__name__, e))
        return 1
    code, line = air_verdict(name, thumb, blocks, len(a.block_at), a.min_blocks)
    log(line)
    return code


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--files", nargs="+", help="local AVI/SER files or glob patterns")
    mode.add_argument("--air", action="store_true", help="check the newest clip on the Air's SMB share")
    ap.add_argument("--expect", choices=["planet", "empty"], help="--files: PASS/FAIL each clip against this")
    ap.add_argument("--every", type=int, default=1, help="--files: test every Nth frame")
    ap.add_argument("--min-planet-frac", type=float, default=0.99, help="--expect planet: fraction of frames that must show it")
    ap.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1), help="--files: clips checked in parallel")
    ap.add_argument("--block", type=int, default=20, help="frames per block mean")
    ap.add_argument("--block-at", type=lambda s: [float(v) for v in s.split(",")], default=None,
                    help="comma-separated fractions of the file for block means (default --files 0.05,0.25,0.5,0.75,0.95; --air 0.05,0.5,0.95)")
    ap.add_argument("--roi", type=int, default=1000, help="frame size to assume when an AVI header is unreadable")
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST", "192.168.1.35"))
    ap.add_argument("--share", default="EMMC Images")
    ap.add_argument("--folder", default="Video")
    ap.add_argument("--mountpoint", default=os.path.join(tempfile.gettempdir(), "asiair_emmc"))
    ap.add_argument("--video-dir", help="--air: read clips from this folder instead of mounting the share (a share you mounted yourself, or pulled files)")
    ap.add_argument("--since", type=stamp, help="--air: YYYY-MM-DD-HHMMSS local; the clip must be named at or after this")
    ap.add_argument("--until", type=stamp, help="--air: YYYY-MM-DD-HHMMSS; the clip must be named at or before this, compared "
                    "with the name as the Air wrote it (no --skew) -- keeps a manual check off the clip still being recorded")
    ap.add_argument("--min-blocks", type=int, default=2, help="--air: PLANET needs at least this many readable block means")
    ap.add_argument("--skew", type=float, default=60, help="--air: seconds of clock difference allowed between this Mac and the Air")
    ap.add_argument("--exclude", action="append", help="--air: a clip name already checked, never to be taken for the new one (repeatable)")
    ap.add_argument("--settle", type=float, default=20, help="--air: seconds to wait for the file to stop growing")
    ap.add_argument("--retries", type=int, default=3, help="--air: share remounts before giving up")
    ap.add_argument("--heartbeat", type=float, default=5.0)
    detector_args(ap)
    a = ap.parse_args()

    if a.air:
        if not a.since:
            ap.error("--air needs --since")
        if a.until and a.until < a.since:
            ap.error("--until %s is before --since %s" % (a.until, a.since))
        a.block_at = a.block_at or [0.05, 0.5, 0.95]
        if not 1 <= a.min_blocks <= len(a.block_at):
            ap.error("--min-blocks must be 1..%d (the number of --block-at fractions)" % len(a.block_at))
        sys.exit(check_air(a))

    a.block_at = a.block_at or [0.05, 0.25, 0.5, 0.75, 0.95]
    paths = []
    for p in a.files:
        hits = sorted(glob.glob(p))
        if not hits:
            ap.error("no file matches %s" % p)
        paths += [h for h in hits if h.lower().endswith((".avi", ".ser"))]
    log("%d clip(s), every %d frame(s), %d-frame means at %s, %d job(s)" % (
        len(paths), a.every, a.block, ",".join("%g" % f for f in a.block_at), a.jobs))
    results = {}
    with ProcessPoolExecutor(max_workers=max(1, min(a.jobs, len(paths)))) as ex:
        futs = {ex.submit(check_file, p, a): p for p in paths}
        for fu in as_completed(futs):
            p = futs[fu]
            try:
                results[p] = fu.result()
            except Exception as e:
                results[p] = e
                log("ERROR %s: %s: %s" % (os.path.basename(p), e.__class__.__name__, e))
            else:
                log("done " + summarise(results[p], a.expect)[1])
    print(flush=True)
    log("SUMMARY")
    allok = True
    for p in paths:
        r = results[p]
        if isinstance(r, Exception):
            log("  ERROR %s: %s" % (os.path.basename(p), r))
            allok = False
            continue
        ok, line = summarise(r, a.expect)
        log("  " + line)
        if r["first_miss"] and a.expect == "planet":
            log("        first frame without the planet: #%d %s" % r["first_miss"])
        allok = allok and ok is not False
    if a.expect:
        log("ALL PASS" if allok else "SOME FAIL")
        sys.exit(0 if allok else 1)
