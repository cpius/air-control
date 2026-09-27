#!/usr/bin/env python3
"""Grab one fresh full-resolution preview frame off the Air and save it as a
16-bit FITS, so tools/framecheck.py can measure it.

    python3 grab.py --host <air-ip> --exp 3 --out frame.fit
"""
import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session import Session


def write_fits(path, buf, w, h, cards):
    hdr = [
        ("SIMPLE", "T"), ("BITPIX", "16"), ("NAXIS", "2"),
        ("NAXIS1", str(w)), ("NAXIS2", str(h)),
        ("BZERO", "32768"), ("BSCALE", "1"),
    ]
    hdr += cards
    lines = []
    for k, v in hdr:
        if v.startswith("'"):
            lines.append("%-8s= %-20s" % (k, v))
        else:
            lines.append("%-8s= %20s" % (k, v))
    lines.append("END")
    head = "".join(s.ljust(80) for s in lines)
    head = head.ljust(((len(head) + 2879) // 2880) * 2880)
    # unsigned 16 -> signed with BZERO 32768, big-endian
    data = bytearray(len(buf) * 2)
    struct.pack_into(">%dh" % len(buf), data, 0, *[v - 32768 for v in buf])
    pad = (-len(data)) % 2880
    with open(path, "wb") as f:
        f.write(head.encode("ascii"))
        f.write(data)
        f.write(b"\0" * pad)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--key", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "embedded_key.pem"))
    ap.add_argument("--exp", type=float, default=3.0)
    ap.add_argument("--gain", type=int, default=250)
    ap.add_argument("--bin", type=int, default=1)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    s = Session(a.host, a.key, with_mount=False)
    try:
        fp = s.c("get_focuser_position")
        s.page("preview", a.exp, a.gain, binning=a.bin)
        time.sleep(a.exp + 1)
        v, w, h = s.fresh(1)
        s.c("stop_exposure")
    finally:
        s.close()
    cards = [("EXPTIME", "%.2f" % a.exp), ("GAIN", str(a.gain)),
             ("XBINNING", str(a.bin)), ("FOCUSPOS", str(fp)),
             ("BAYERPAT", "'RGGB    '"), ("DATE-OBS", "'%s'" % time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()))]
    write_fits(a.out, v, w, h, cards)
    print("wrote %s  %dx%d  focuser %s  max %d" % (a.out, w, h, fp, max(v)))


if __name__ == "__main__":
    sys.exit(main())
