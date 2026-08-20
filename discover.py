#!/usr/bin/env python3
"""Find the ASI585MC Air on the network and fingerprint what it's listening on.

Three independent methods, because any one of them can come up empty:

  1. ASCOM Alpaca discovery  - UDP broadcast, the documented standard.
  2. mDNS / Bonjour          - shells out to dns-sd, ZWO gear often advertises.
  3. TCP port sweep          - brute force, works even when discovery is broken.

Usage:
    python3 discover.py                     # all methods, auto-detect subnet
    python3 discover.py --subnet 10.0.0     # e.g. when joined to the Air's own AP
    python3 discover.py --host 192.168.2.50 # fingerprint one known host
"""

import argparse
import concurrent.futures
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from airlog import add_log_args, configure_logging, get_logger

log = get_logger("discover")

# Ports the ASIAIR firmware family is known or suspected to use.
PORTS = {
    22: "ssh",
    80: "http",
    4400: "asiair (aux)",
    4500: "asiair (guide stream)",
    4700: "asiair RPC  <-- main control channel",
    4800: "asiair (image/preview stream)",
    4350: "OTA command",
    4360: "OTA data",
    4361: "OTA data (legacy)",
    5000: "http (alt)",
    8080: "http (alt)",
    11111: "ASCOM Alpaca (standard port — this Air does NOT use it)",
    32323: "ASCOM Alpaca  <-- documented REST API, the port this Air serves",
}

ALPACA_DISCOVERY_PORT = 32227
ALPACA_DISCOVERY_MSG = b"alpacadiscovery1"


def local_ipv4():
    """Best-guess primary IPv4 address of this machine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def alpaca_discover(timeout=3.0):
    """Broadcast the standard Alpaca discovery packet and collect replies."""
    found = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(timeout)

    targets = ["255.255.255.255"]
    try:
        ip = ipaddress.ip_interface(f"{local_ipv4()}/24")
        targets.append(str(ip.network.broadcast_address))
    except Exception:
        pass

    for t in set(targets):
        try:
            s.sendto(ALPACA_DISCOVERY_MSG, (t, ALPACA_DISCOVERY_PORT))
        except OSError as e:
            print(f"  ! broadcast to {t} failed: {e}")

    with log.slow("listening for Alpaca replies (%.0fs)" % timeout, quiet_for=1.0,
                  detail=lambda: "%d replied" % len(found)):
        while True:
            try:
                data, addr = s.recvfrom(1024)
            except socket.timeout:
                break
            try:
                port = json.loads(data.decode()).get("AlpacaPort")
            except Exception:
                port = None
            log.info("Alpaca reply from %s (AlpacaPort=%s)", addr[0], port)
            found[addr[0]] = port
    s.close()
    return found


def mdns_browse(timeout=4):
    """Ask Bonjour what's advertising itself. Alpaca uses _alpaca._tcp."""
    out = []
    # Three dns-sd calls, each of which runs to its own timeout — 12s by
    # default, all of it looking like a hang.
    svcs = ("_alpaca._tcp", "_http._tcp", "_asiair._tcp")
    done = [0]
    with log.slow("mDNS browse (%d services x %ds)" % (len(svcs), timeout),
                  quiet_for=1.0,
                  detail=lambda: "%d/%d browsed, %d record(s)"
                                 % (done[0], len(svcs), len(out))):
        for svc in svcs:
            try:
                r = subprocess.run(
                    ["dns-sd", "-B", svc, "local"],
                    capture_output=True, text=True, timeout=timeout,
                )
                text = r.stdout
            except subprocess.TimeoutExpired as e:
                text = e.stdout.decode() if e.stdout else ""
            except FileNotFoundError:
                return ["dns-sd not available"]
            for line in text.splitlines():
                if "Add" in line and "_tcp" in line:
                    out.append(f"{svc}: {line.strip()}")
            done[0] += 1
    return out


def probe(host, port, timeout=0.6):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def scan_host(host, ports=None):
    ports = ports or list(PORTS)
    open_ports = []
    done = [0]
    with log.slow("fingerprinting %s (%d ports)" % (host, len(ports)),
                  quiet_for=1.0,
                  detail=lambda: "%d/%d probed, %d open"
                                 % (done[0], len(ports), len(open_ports))):
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
            futs = {ex.submit(probe, host, p): p for p in ports}
            for f in concurrent.futures.as_completed(futs):
                done[0] += 1
                if f.result():
                    open_ports.append(futs[f])
    return sorted(open_ports)


def sweep(subnet, key_ports=(4700, 32323, 11111)):
    """Sweep a /24 for the ports that positively identify an Air.

    32323 matters: this Air serves Alpaca there, not on the ASCOM-default
    11111 (see alpaca.py). Sweeping only 4700 and 11111 finds nothing when
    the RPC channel is down but Alpaca is up. 11111 stays in the list so a
    stock-port Alpaca server would still be spotted.
    """
    hits = {}
    hosts = [f"{subnet}.{i}" for i in range(1, 255)]
    total = len(hosts) * len(key_ports)
    done = [0]
    log.info("sweeping %s.0/24 on ports %s (%d probes)", subnet,
             ", ".join(str(p) for p in key_ports), total)
    with log.slow("TCP sweep", quiet_for=1.0,
                  detail=lambda: "%d/%d probes, %d host(s) answering"
                                 % (done[0], total, len(hits))):
        with concurrent.futures.ThreadPoolExecutor(max_workers=200) as ex:
            futs = {ex.submit(probe, h, p, 0.4): (h, p)
                    for h in hosts for p in key_ports}
            for f in concurrent.futures.as_completed(futs):
                h, p = futs[f]
                done[0] += 1
                if f.result():
                    log.info("%s has %d open", h, p)
                    hits.setdefault(h, []).append(p)
    return {h: sorted(ps) for h, ps in hits.items()}


def report_host(host):
    print(f"\n=== {host} ===")
    open_ports = scan_host(host)
    if not open_ports:
        print("  no candidate ports open")
        return
    for p in open_ports:
        print(f"  {p:<6} open   {PORTS.get(p, '')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subnet", help="e.g. 192.168.2 (defaults to this Mac's /24)")
    ap.add_argument("--host", help="fingerprint a single host and skip the sweep")
    add_log_args(ap)
    args = ap.parse_args()
    configure_logging(args)

    if args.host:
        report_host(args.host)
        return

    me = local_ipv4()
    subnet = args.subnet or me.rsplit(".", 1)[0]
    print(f"This Mac: {me}   scanning {subnet}.0/24\n")

    print("[1] ASCOM Alpaca UDP discovery ...")
    alpaca = alpaca_discover()
    if alpaca:
        for ip, port in alpaca.items():
            print(f"    FOUND {ip}  AlpacaPort={port}")
    else:
        print("    (none replied)")
    print()

    print("[2] mDNS / Bonjour ...")
    lines = mdns_browse()
    print("\n".join(f"    {l}" for l in lines) if lines else "    (nothing advertised)")
    print()

    print("[3] TCP sweep for ports 4700 (ASIAIR RPC) and 32323/11111 (Alpaca) ...")
    hits = sweep(subnet)
    if not hits:
        print("    nothing found — is the Air powered on and on this network?")
        print("    If it's in its own AP mode, join its Wi-Fi and retry with")
        print("    --subnet 10.0.0")
        return
    for h, ps in hits.items():
        print(f"    FOUND {h}  ports {ps}")
    for h in hits:
        report_host(h)


if __name__ == "__main__":
    sys.exit(main())
