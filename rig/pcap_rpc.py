#!/usr/bin/env python3
"""Turn a tcpdump capture of the ASIAIR app into a readable RPC transcript.

When the app can do something these scripts cannot (the mount attach that
clears goto error 300 is the case that prompted this), capture the app doing it
and read the wire -- never guess method names (see docs/RPC_METHODS.md):

    sudo tcpdump -i en0 -n -s0 -U -w app.pcap host <air-ip>
    python3 rig/pcap_rpc.py --pcap app.pcap                 # app -> Air calls + replies
    python3 rig/pcap_rpc.py --pcap app.pcap --events        # add Air -> app events
    python3 rig/pcap_rpc.py --pcap app.pcap --ports 4400    # one channel

Payloads on 4400/4700 are plaintext JSON lines (4700's RSA is only the
handshake), so no decryption is needed. 4800 (images) is binary and skipped.
Reads classic pcap and pcapng, Ethernet or loopback, IPv4 TCP; no third-party
packages.
"""
import argparse, json, struct, sys, datetime

NOISE = {"PiStatus", "Temperature", "Version", "GuideStep", "AutoGoto_Step_Info",
         "test_connection", "pi_is_verified"}


def packets(path):
    """Yield (ts, linktype, frame bytes) from pcap or pcapng."""
    with open(path, "rb") as f:
        data = f.read()
    magic = data[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d"):
        le = magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1")
        nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
        e = "<" if le else ">"
        linktype = struct.unpack(e + "I", data[20:24])[0]
        off = 24
        while off + 16 <= len(data):
            sec, frac, incl, _ = struct.unpack(e + "IIII", data[off:off + 16])
            off += 16
            yield sec + frac / (1e9 if nano else 1e6), linktype, data[off:off + incl]
            off += incl
        return
    if magic == b"\x0a\x0d\x0d\x0a":                     # pcapng
        off, e, ifaces = 0, "<", []
        while off + 12 <= len(data):
            btype = struct.unpack(e + "I", data[off:off + 4])[0]
            if btype == 0x0A0D0D0A:
                e = "<" if data[off + 8:off + 12] == b"\x4d\x3c\x2b\x1a" else ">"
                ifaces = []
            blen = struct.unpack(e + "I", data[off + 4:off + 8])[0]
            body = data[off + 8:off + blen - 4]
            if btype == 1:                               # interface description
                ifaces.append(struct.unpack(e + "H", body[:2])[0])
            elif btype == 6:                             # enhanced packet
                iid, hi, lo, cap, _ = struct.unpack(e + "IIIII", body[:20])
                lt = ifaces[iid] if iid < len(ifaces) else 1
                yield ((hi << 32) | lo) / 1e6, lt, body[20:20 + cap]
            off += blen
        return
    sys.exit("not a pcap/pcapng file")


def tcp_segments(path):
    """Yield (ts, src, sport, dst, dport, seq, payload) for IPv4 TCP."""
    for ts, lt, fr in packets(path):
        if lt == 1:                                      # Ethernet
            etype, ip = struct.unpack(">H", fr[12:14])[0], fr[14:]
            if etype == 0x8100:
                etype, ip = struct.unpack(">H", fr[16:18])[0], fr[18:]
            if etype != 0x0800:
                continue
        elif lt in (0, 108):                             # BSD loopback / null
            ip = fr[4:]
        else:
            continue
        if not ip or ip[0] >> 4 != 4 or ip[9] != 6:
            continue
        ihl = (ip[0] & 15) * 4
        tot = struct.unpack(">H", ip[2:4])[0]
        src, dst = ".".join(map(str, ip[12:16])), ".".join(map(str, ip[16:20]))
        tcp = ip[ihl:tot]
        sport, dport, seq = struct.unpack(">HHI", tcp[:8])
        doff = (tcp[12] >> 4) * 4
        payload = tcp[doff:]
        if payload:
            yield ts, src, sport, dst, dport, seq, payload


def streams(path, air, ports):
    """Reassemble each direction of each connection; split into JSON lines."""
    flows = {}
    for ts, src, sport, dst, dport, seq, pl in tcp_segments(path):
        if src == air and sport in ports:
            key, outbound, port = (dst, dport, sport), False, sport
        elif dst == air and dport in ports:
            key, outbound, port = (src, sport, dport), True, dport
        else:
            continue
        fl = flows.setdefault((key, outbound), {"port": port, "segs": {}})
        fl["segs"].setdefault(seq, (ts, pl))             # first copy wins (retransmits)
    msgs = []
    for (key, outbound), fl in flows.items():
        buf, t_first, nxt = b"", None, None
        for seq in sorted(fl["segs"]):
            ts, pl = fl["segs"][seq]
            if nxt is not None and seq < nxt:            # overlap
                pl = pl[nxt - seq:]
                if not pl:
                    continue
            nxt = seq + len(fl["segs"][seq][1])
            buf += pl
            while b"\r\n" in buf or b"\n" in buf:
                cut = buf.find(b"\n")
                line, buf = buf[:cut].strip(), buf[cut + 1:]
                if line:
                    msgs.append((t_first or ts, fl["port"], key[1], outbound, line))
                t_first = None
            if buf and t_first is None:
                t_first = ts
    msgs.sort(key=lambda m: m[0])
    return msgs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pcap", required=True)
    ap.add_argument("--air", default=None, help="Air IP (default: guessed from port 4400/4700 traffic)")
    ap.add_argument("--ports", default="4400,4700,4900")
    ap.add_argument("--events", action="store_true", help="also print Air->app events")
    ap.add_argument("--all", action="store_true", help="do not hide routine chatter")
    ap.add_argument("--width", type=int, default=400)
    a = ap.parse_args()
    ports = {int(p) for p in a.ports.split(",")}
    air = a.air
    if not air:
        for _, src, sport, dst, dport, _, _ in tcp_segments(a.pcap):
            if sport in ports:
                air = src; break
            if dport in ports:
                air = dst; break
    if not air:
        sys.exit("no traffic on ports %s" % sorted(ports))
    print("Air %s, ports %s" % (air, sorted(ports)))
    t0 = None
    for ts, port, cport, outbound, line in streams(a.pcap, air, ports):
        t0 = t0 or ts
        try:
            j = json.loads(line)
        except Exception:
            if outbound or a.all:
                print("%8.3f %s %d c%-5d %s" % (ts - t0, ">>" if outbound else "<<", port, cport,
                                               line[:a.width]))
            continue
        name = j.get("method") or j.get("Event") or ""
        if not a.all and name in NOISE:
            continue
        is_event = "Event" in j
        if is_event and not (a.events or a.all):
            continue
        tag = ">>" if outbound else ("ev" if is_event else "<<")
        clock = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
        print("%s %8.3f %s %d c%-5d %s" % (clock, ts - t0, tag, port, cport,
                                          json.dumps(j, separators=(",", ":"))[:a.width]))


if __name__ == "__main__":
    main()
