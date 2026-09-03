#!/usr/bin/env python3
"""Generate a focus sweep and a preview frame in the real on-disk formats.

So the dashboard can be laid out, and its panes verified, with the rig packed
away. The files written are byte-compatible with what `focus.py`'s Writer and
the preview path actually produce -- same step%03d.json keys, same PNG closeups,
same reject labels -- so a pane that renders these renders the real thing.

Writes under dashboard/demo/, which nothing else looks at. Delete the directory
and the dashboard falls back to real data with no configuration change.

PNGs are encoded here in pure stdlib (zlib + struct). PIL is not installed on
this machine, and the rest of the toolkit is deliberately stdlib-only.

    python3 demo_data.py            # write it
    python3 demo_data.py --clean    # remove it
"""

import argparse
import datetime
import json
import math
import os
import random
import shutil
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(os.path.expanduser("~/ASICAP"), "dashboard", "demo")
RNG = random.Random(585)

BEST = 11077          # the focus position measuring-focus-and-drift landed on


def png(path, rows, w, h):
    """Greyscale 8-bit PNG. rows is a list of h bytes-objects of length w."""
    raw = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(raw, 6)))
        f.write(chunk(b"IEND", b""))


def star_tile(w, h, fwhm, peak=52000, bg=980, nstars=1):
    """A Gaussian star on a noisy background, scaled to 8-bit for the closeup."""
    sig = fwhm / 2.3548
    cx, cy = w / 2.0, h / 2.0
    extra = [(RNG.uniform(0, w), RNG.uniform(0, h), RNG.uniform(0.04, 0.15))
             for _ in range(nstars - 1)]
    rows = []
    for y in range(h):
        row = bytearray(w)
        for x in range(w):
            v = bg + RNG.gauss(0, 22)
            v += peak * math.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (2 * sig * sig))
            for sx, sy, amp in extra:
                v += peak * amp * math.exp(-((x - sx) ** 2 + (y - sy) ** 2)
                                           / (2 * sig * sig))
            row[x] = max(0, min(255, int(v / 65535.0 * 255 * 4)))
        rows.append(row)
    return rows


def focus_run(outdir, n=11, span=500):
    """A V-curve sweep, including the two rejects a real sweep always has."""
    os.makedirs(outdir, exist_ok=True)
    lo = BEST - span // 2
    step_px = span // (n - 1)
    t0 = datetime.datetime.now() - datetime.timedelta(minutes=9)
    W = H = 240
    written = 0
    for i in range(n):
        pos = lo + i * step_px
        # Real V-curve: defocus adds to the in-focus width roughly linearly.
        width = 3.71 + abs(pos - BEST) / 96.0 + RNG.uniform(-0.05, 0.05)
        peak = int(max(2400, 52000 * (3.71 / width) ** 2))
        flux = peak * width * 2.4
        rejected, label = None, None
        if i == 2:
            rejected, label = "jump", "A%d REJECT jump41px" % pos
        elif i == n - 2:
            rejected, label = "saturated", "A%d REJECT saturated" % pos
        written += 1
        base = os.path.join(outdir, "step%03d" % written)
        rec = {"step": written, "label": label or "A%d pk%d w%.1f" % (pos, peak, width),
               "width": 1920, "height": 1080,
               "star_x": 964 + RNG.randint(-3, 3), "star_y": 531 + RNG.randint(-3, 3),
               "itemsize": 2, "dtype": "uint16", "order": "row-major",
               "time": (t0 + datetime.timedelta(seconds=i * 46)).isoformat(
                   timespec="seconds"),
               "focuser": pos, "requested": pos, "exposure_s": 2.0, "gain": 100,
               "jump_px": 41.0 if rejected == "jump" else round(RNG.uniform(0.4, 3.1), 1)}
        if rejected:
            rec["rejected"] = rejected
        else:
            rec.update(peak=peak, star_width_px=round(width, 2),
                       flux=round(flux, 1), score=round(peak / width, 1))
        json.dump(rec, open(base + ".json", "w"), indent=1)
        png(base + ".png", star_tile(W, H, max(1.8, width * 1.5),
                                     peak=65000 if rejected == "saturated" else peak,
                                     nstars=3), W, H)
    return outdir


def preview(path, w=640, h=420):
    """A wide star field, standing in for the most recent frame off the camera."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stars = [(RNG.uniform(0, w), RNG.uniform(0, h),
              RNG.uniform(0.05, 1.0) ** 3, RNG.uniform(1.6, 2.6)) for _ in range(260)]
    rows = []
    for y in range(h):
        row = bytearray(w)
        for x in range(w):
            v = 26 + RNG.gauss(0, 3.2)
            v += 14 * math.exp(-((y - h * 0.62) ** 2) / (2 * (h * 0.4) ** 2))
            row[x] = max(0, min(255, int(v)))
        rows.append(row)
    for sx, sy, amp, fw in stars:
        sig = fw / 2.3548
        r = int(fw * 3)
        for dy in range(-r, r + 1):
            y = int(sy) + dy
            if not 0 <= y < h:
                continue
            for dx in range(-r, r + 1):
                x = int(sx) + dx
                if not 0 <= x < w:
                    continue
                g = 235 * amp * math.exp(-(dx * dx + dy * dy) / (2 * sig * sig))
                rows[y][x] = max(0, min(255, int(rows[y][x] + g)))
    png(path, rows, w, h)
    return path


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--clean", action="store_true")
    a = p.parse_args()
    if a.clean:
        shutil.rmtree(DEMO, ignore_errors=True)
        print("removed", DEMO)
        return
    d = focus_run(os.path.join(DEMO, "focus-demo"))
    print("focus run  ", d, "(%d steps)" % len(os.listdir(d)))
    p2 = preview(os.path.join(DEMO, "preview", "NGC6946-preview.png"))
    print("preview    ", p2)
    print("\nthese are SYNTHETIC. delete with: python3 demo_data.py --clean")


if __name__ == "__main__":
    main()
