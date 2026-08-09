# Controlling the ASI585MC Air

Findings from probing the device on 2026-08-09, plus a stdlib-only toolkit.
No dependencies — `python3 <script>` works as-is.

## The device

| | |
|---|---|
| Address | `192.168.2.149` (does **not** answer ping — ICMP is off, use a TCP scan) |
| Alpaca | port **32323**, not the 11111 default. Discovery via UDP 32227 works. |
| ASIAIR RPC | port 4700, open but authentication-gated (see below) |
| Firmware | 43.97 (`svr_ver_int` 29, identifies as `"ASI AIR imager"`) |
| Also open | 22 (ssh), 4350/4360 (OTA), 4400, 4500 (guide stream), 4800 (image stream) |

Two cameras are exposed as separate Alpaca devices:

| # | Device | Sensor | Pixel | Max exp |
|---|---|---|---|---|
| 0 | ZWO ASI220MM Air | 1920x1080 mono (guide) | 4.0 µm | 10 s |
| 1 | ZWO ASI585MC Air | 3840x2160 Bayer RGGB | 2.9 µm | 2000 s |

Gain range 0–600 on both. No cooling (`cooleron` false, and there's no cooler).

## Two ways in

### 1. ASCOM Alpaca — works now, vendor-supported

Plain HTTP REST, no auth. ZWO advertise this for the 585 Air, so it won't break
on a firmware bump. Verified working: connect, gain/offset, binning, subframe,
`startexposure`, `imageready`, `imagearray`, temperature.

This covers **the camera only** — it is not a route to mount control, plate
solving, guiding, or autorun.

### 2. Native ASIAIR RPC on 4700 — unlocked via handshake

Newline-delimited JSON-RPC; the channel the ASIAIR app itself uses, and where
mount goto / plate solve / autorun / guiding live. It connects and streams
telemetry unprompted (`Version`, `Station` wifi RSSI, `PiStatus` temp ~41 °C),
and answers exactly two methods before authentication:

- `test_connection` → `"server connected!"`
- `get_verify_str` → `{"str": "<32-char challenge>"}`

Every other method is **silently dropped** — no error, just no reply. That
silence is the signal that auth is required.

Firmware 7.18+ added a two-step handshake. It needs an RSA private key that
lives in ZWO's app; `seestar_alp` implements the flow but ships none
(`interop_pem` defaults to empty), leaving the user to supply one — which its
config justifies under the DMCA interoperability exemption
(17 U.S.C. § 1201(f)). With a key in `embedded_key.pem`, the handshake is:

1. `get_verify_str` → `{"str": "<challenge>"}`
2. sign the challenge bytes **RSA PKCS#1 v1.5 / SHA-1**, base64-encode
3. `verify_client([<signature>, <challenge>])` → `0`
4. `pi_is_verified` → `True`, and the gated methods answer

Note the param shape: `verify_client` takes **two positional strings**
`[signature, challenge]` — *not* the `{"sign", "data"}` object shape you might
guess (that returns `105: expected string param`). Signing needs the
`cryptography` package; the rest of the toolkit stays stdlib-only.

Verification is **per-connection** — it lives on the socket, so every new
connection re-runs the handshake. `air_rpc.py --key` (and `Air(host, key=...)`)
do this automatically on connect; `handshake.py` runs it standalone and dumps
`get_device_state` as proof.

## Files

| File | What it does |
|---|---|
| `discover.py` | Finds the Air: Alpaca UDP discovery, mDNS, TCP sweep. Use `--subnet 10.0.0` if you join the Air's own AP. |
| `alpaca.py` | Alpaca client + CLI. Importable as a library (`from alpaca import Alpaca`). |
| `air_rpc.py` | Raw 4700 JSON-RPC: `probe`, `call`, `console`, `listen`. `--key` runs the auth handshake to unlock gated methods. |
| `mount.py` | **AM5N mount control on port 4400** (no auth): `info`, `coord`, `track on/off`, `goto`, `sync`, `park`. |
| `guide.py` | **Guiding on port 4400**: `state`, `connect`, `expose`, `loop`, `start`, `stop`. Unprefixed guide methods. |
| `solve_center.py` | **Plate-solve-and-center** via native `start_auto_goto` on 4700 (needs key); horizon-guarded. |
| `handshake.py` | Standalone auth handshake with a supplied RSA key; proves it by reading `get_device_state`. |
| `find_methods.py` | Enumerate registered RPC methods via the 103-vs-else oracle. |
| `RPC_METHODS.md` | Full method map (4700 + 4400), extracted from the app and validated live. |
| `smoke_test.py` | End-to-end: connect, subframe, expose, read pixels. |
| `embedded_key.pem` | RSA private key for the 4700 handshake (user-supplied; not committed). |

**Ports:** `4700` = camera/focuser/solve/settings (RSA-gated, use `--key`).
`4400` = **mount + guiding, no auth** — this is where `scope_*` lives. Mount
commands returning `103` on 4700 aren't gated; they're just on the wrong port.

`air_rpc.py` keeps read-only methods (`PROBE_METHODS`) separate from
state-changing ones (`WRITE_METHODS`); `probe` only ever sends the former.

## Recipes

```bash
python3 discover.py                                    # find it
python3 alpaca.py --host 192.168.2.149 info
python3 alpaca.py --host 192.168.2.149 get camera 1 ccdtemperature
python3 alpaca.py --host 192.168.2.149 put camera 1 gain Gain=252
python3 smoke_test.py                                  # take a real frame
python3 air_rpc.py --host 192.168.2.149 listen --seconds 60   # watch telemetry

# native 4700 channel, unlocked with the RSA key:
python3 handshake.py --host 192.168.2.149 --key embedded_key.pem
python3 air_rpc.py --host 192.168.2.149 --key embedded_key.pem probe
python3 air_rpc.py --host 192.168.2.149 --key embedded_key.pem call get_device_state

# mount on port 4400 (no key needed):
python3 mount.py info                    # ZWO AM5N, fw 1.8.6
python3 mount.py coord                    # live RA/Dec/Alt/Az + tracking
python3 mount.py track on                 # start sidereal tracking
python3 mount.py goto 20.016 35.365       # slew to RA 20.016h, Dec 35.365°

# plate-solve-and-center (goto accuracy; 4700 + key, needs stars):
python3 solve_center.py 20.016 35.365     # refuses below-horizon targets

# guiding (port 4400; calibration needs a guide star):
python3 guide.py state                    # Idle/Looping/Guiding/…
python3 guide.py connect on; python3 guide.py expose 1000; python3 guide.py loop
```

Note `imagearray` returns JSON, so a full 3840x2160 frame is 8.3M numbers and
is slow. Use binning or a subframe for anything interactive; for real captures
send `Accept: application/imagebytes` to get the binary form instead.
